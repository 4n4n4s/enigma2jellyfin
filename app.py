import os
import requests
import requests_cache
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import urllib.parse
import click
import threading
import time
from flask import Flask, send_file, abort

app = Flask(__name__)

# Globals to store file paths and config
CONFIG = {}
FILES = {}

def ensure_data_dir():
    if not os.path.exists("data"):
        os.makedirs("data")

def get_bouquet_reference(host, port, bouquet_name):
    url = f"http://{host}:{port}/web/getservices"
    resp = requests.get(url)
    tree = ET.fromstring(resp.content)
    for service in tree.findall("e2service"):
        ref = service.findtext("e2servicereference", default="")
        if bouquet_name in ref:
            return ref
    return None

def get_channels_from_bouquet(host, port, bouquet_ref):
    sref_encoded = urllib.parse.quote(bouquet_ref, safe="")
    url = f"http://{host}:{port}/web/getservices?sRef={sref_encoded}"
    resp = requests.get(url)
    tree = ET.fromstring(resp.content)
    return [
        {
            "name": svc.findtext("e2servicename"),
            "ref": svc.findtext("e2servicereference")
        }
        for svc in tree.findall("e2service")
        if svc.findtext("e2servicereference", "").startswith("1:0")
    ]

def fetch_epg(host, port, service_ref):
    sref_encoded = urllib.parse.quote(service_ref, safe="")
    url = f"http://{host}:{port}/web/epgservice?sRef={sref_encoded}"
    resp = requests.get(url)
    tree = ET.fromstring(resp.content)
    events = []
    for ev in tree.findall("e2event"):
        try:
            start = int(ev.findtext("e2eventstart"))
            duration = int(ev.findtext("e2eventduration"))
            events.append({
                "title": ev.findtext("e2eventtitle", default=""),
                "desc": ev.findtext("e2eventdescription", default=""),
                "start": datetime.fromtimestamp(start, tz=timezone.utc),
                "end": datetime.fromtimestamp(start + duration, tz=timezone.utc)
            })
        except Exception:
            continue
    return events

def safe_channel_id(service_ref):
    return "".join(c if c.isalnum() else "_" for c in service_ref)

def write_epg_xml(channels, filename, host, port):
    tv = ET.Element("tv", attrib={"generator-info-name": "enigma2jellyfin"})
    base_url = f"http://{host}:{port}"

    for ch in channels:
        chan_id = safe_channel_id(ch["ref"][:-1])
        ch_elem = ET.SubElement(tv, "channel", id=chan_id)
        ET.SubElement(ch_elem, "display-name").text = ch["name"]
        ET.SubElement(ch_elem, "icon", src=f"{base_url}/picon/{chan_id}.png")

        for prog in ch["epg"]:
            prog_elem = ET.SubElement(
                tv, "programme",
                start=prog["start"].strftime("%Y%m%d%H%M%S +0000"),
                stop=prog["end"].strftime("%Y%m%d%H%M%S +0000"),
                channel=chan_id
            )
            ET.SubElement(prog_elem, "title", lang="en").text = prog["title"]
            ET.SubElement(prog_elem, "desc", lang="en").text = prog["desc"]

    tree = ET.ElementTree(tv)
    tree.write(filename, encoding="utf-8", xml_declaration=True)
    print(f"✅ Wrote EPG XML to {filename}")

def extract_program_id(service_ref):
    try:
        parts = service_ref.split(":")
        if len(parts) >= 4:
            hex_id = parts[3]
            return int(hex_id, 16)
    except Exception:
        pass
    return None

def write_m3u(channels, filename, host, streamport):
    lines = ["#EXTM3U"]
    base_url = f"http://{host}:{streamport}"

    for ch in channels:
        chan_id = safe_channel_id(ch["ref"][:-1])
        logo = f"{base_url}/picon/{chan_id}.png"
        stream = f"{base_url}/{ch['ref']}"
        pid = extract_program_id(ch['ref'])

        lines.append("#EXTVLCOPT:http-reconnect=true")
        lines.append(f'#EXTINF:-1 tvg-id="{chan_id}" tvg-name="{ch["name"]}" tvg-logo="{logo}", {ch["name"]}')
        if pid:
            lines.append(f'#EXTVLCOPT:program={pid}')
        lines.append(stream)

    with open(filename, "w") as f:
        f.write("\n".join(lines))
    print(f"✅ Wrote M3U playlist to {filename}")

def generate_files():
    host = CONFIG["host"]
    port = CONFIG["port"]
    streamport = CONFIG["streamport"]
    bouquet = CONFIG["bouquet"]
    epg_file = CONFIG["epg_file"]
    m3u_file = CONFIG["m3u_file"]

    print("🔄 Generating EPG and M3U files...")
    bouquet_ref = get_bouquet_reference(host, port, bouquet)
    if not bouquet_ref:
        print(f"❌ Bouquet '{bouquet}' not found.")
        return

    channels = get_channels_from_bouquet(host, port, bouquet_ref)

    for ch in channels:
        ch["epg"] = fetch_epg(host, port, ch["ref"])

    write_epg_xml(channels, epg_file, host, port)
    write_m3u(channels, m3u_file, host, streamport)
    print("✅ Generation complete.")

def schedule_job(interval_minutes):
    while True:
        try:
            generate_files()
        except Exception as e:
            print(f"❌ Error during generation: {e}")
        time.sleep(interval_minutes * 60)

@app.route("/epg.xml")
def serve_epg():
    try:
        return send_file(CONFIG["epg_file"])
    except Exception:
        abort(404)

@app.route("/playlist.m3u")
def serve_m3u():
    try:
        return send_file(CONFIG["m3u_file"])
    except Exception:
        abort(404)

@click.command()
@click.option("--host", envvar="ENIGMA2_HOST", default="10.0.0.101", help="Enigma2 box IP or hostname")
@click.option("--port", envvar="ENIGMA2_PORT", default=80, help="OpenWebIf port")
@click.option("--streamport", envvar="ENIGMA2_STREAMPORT", default=8001, help="OpenWebIf stream port")
@click.option("--bouquet", envvar="BOUQUET", default="userbouquet.f52ab.tv", help="Bouquet name")
@click.option("--epg-file", envvar="EPG_FILE", default="data/epg.xml", help="Output XMLTV filename")
@click.option("--m3u-file", envvar="M3U_FILE", default="data/playlist.m3u", help="Output M3U filename")
@click.option("--interval", envvar="REFRESH_INTERVAL", default=60, help="Regeneration interval in minutes")
@click.option("--http-port", envvar="HTTP_PORT", default=8080, help="Port to serve HTTP files")
def main(host, port, streamport, bouquet, epg_file, m3u_file, interval, http_port):
    global CONFIG
    ensure_data_dir()

    # Install requests cache in data directory
    requests_cache.install_cache('data/enigma2_cache', expire_after=60*60*24)

    CONFIG = {
        "host": host,
        "port": port,
        "streamport": streamport,
        "bouquet": bouquet,
        "epg_file": epg_file,
        "m3u_file": m3u_file,
    }
    print(f"Starting with config: {CONFIG}")
    print(f"Regeneration interval: {interval} minutes")
    print(f"Serving files on http://0.0.0.0:{http_port}/")

    # Initial generation
    try:
        generate_files()
    except Exception as e:
        print(f"❌ Error during generation: {e}")

    # Start scheduled regeneration in background thread
    thread = threading.Thread(target=schedule_job, args=(interval,), daemon=True)
    thread.start()

    # Run Flask app
    app.run(host="0.0.0.0", port=http_port)

if __name__ == "__main__":
    main()
