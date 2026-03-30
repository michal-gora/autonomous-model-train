import network
import ujson

def load_wifi_config():
    try:
        with open("wifi_config.json") as f:
            cfg = ujson.load(f)
            return cfg["ssid"], cfg["password"]
    except Exception as e:
        print("❌ Could not load Wi-Fi config:", e)
        return None, None

def network_connect() -> bool :
    import network
    from utime import sleep,sleep_ms
    network.hostname("train")
    wlan = network.WLAN(network.STA_IF)
    if wlan.isconnected():
        print('[Network] already connected')
        return True
    
    # enable and connect wlan
    ssid, pw = load_wifi_config()
    print(f"Connecting to network with SSID: {ssid}")
    wlan.active(True)
    wlan.connect(ssid,pw)

    # wait for the connection to establish
    sleep(5)
    for i in range(0,100):
        if not wlan.isconnected() and wlan.status() >= 0:
            print("[Network] Waiting to connect..")
            sleep(2)

    # check connection
    if not wlan.isconnected():
        print("[Network] Connection failed!")
        return False
    else:
        print(f"Connected to Wifi as {wlan.ifconfig()[0]}")
        return True



