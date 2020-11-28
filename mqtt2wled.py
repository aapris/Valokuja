import argparse
import datetime as dt
import glob
import json
import logging
import os

import configparser
import paho.mqtt.client as mqtt
import pytz
import requests
import threading

TIMER = None
SIMULATE = False
AUTOCHANGE = 300
B2_LAST_STATE = -1
B2_LAST_PRESSED = None
B3_LAST_STATE = -1
B3_LAST_PRESSED = None
PRESETS1 = []
CURRENT_PRESET1 = 0
PRESETS2 = []
CURRENT_PRESET2 = 0
WLED_IPS = ['172.24.1.201', '172.24.1.202', '172.24.1.203', '172.24.1.204']


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("-l", "--log", dest="log", choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
                        default='ERROR', help="Set the logging level")
    parser.add_argument('-q', '--quiet', action='store_true', help='Never print a char (except on crash)')
    parser.add_argument('-f', '--format', required=True,
                        choices=['jsonsensor', 'ruuvi', 'ruuvitag_collector', 'sensornode'],
                        help='MQTT message format')
    parser.add_argument("--autochange", type=float, default=300, help="Seconds to change to next default effect")
    parser.add_argument("--simulate", action='store_true', help="If set, do not send actual HTTP requests to leds")
    parser.add_argument("--presets", help="Preset directory", required=True)
    parser.add_argument("--config", help="Configuration file", default="config.ini", nargs='?')
    parser.add_argument('-t', '--topic', required=True, nargs='+', help='MQTT topics')
    parser.add_argument("--mqtt_username", help="MQTT user name", nargs='?')
    parser.add_argument("--mqtt_password", help="MQTT password", nargs='?')
    parser.add_argument("--mqtt_host", help="MQTT host", nargs='?')
    parser.add_argument("--mqtt_port", help="MQTT port", nargs='?')
    args = parser.parse_args()
    if args.log:
        logging.basicConfig(format='%(asctime)s %(levelname)-8s %(message)s',
                            level=getattr(logging, args.log))
    return args


def load_presets(args):
    global PRESETS1, PRESETS2
    preset_files = glob.glob(f'{args.presets}/preset-0*.json')
    for pf in preset_files:
        with open(pf, 'rt') as f:
            PRESETS1.append(f.read())
    preset_files = glob.glob(f'{args.presets}/preset-1*.json')
    for pf in preset_files:
        with open(pf, 'rt') as f:
            PRESETS2.append(f.read())
    print(len(PRESETS1), len(PRESETS2))


def on_connect(client, userdata, flags, rc):
    logging.info("Connected with result code {}".format(rc))
    # Subscribing in on_connect() means that if we lose the connection and
    # reconnect then subscriptions will be renewed.
    for t in client.args.topic:
        logging.info(f'Subscribe to {t}')
        client.subscribe(t)


# The callback for when a PUBLISH message is received from the server.
def on_message(client, userdata, msg):
    payload = msg.payload.decode('utf-8')
    if msg.retain == 1:
        logging.info("Do not handle retain message {}".format(payload))
        return
    logging.debug("{} '{}'".format(msg.topic, payload))
    handle_jsonsensor(client, userdata, msg, payload)


def next_effect():
    response = send_telegram(f'Next effect', 1)


def handle_jsonsensor(client, userdata, msg, payload):
    """
    {'sensor': 'button', 'mac': '80:7D:3A:47:6A:F2', 'id': '200101', 'data': {'b1': 0, 'b2': 1, 'b3': 0}}
    """
    global B2_LAST_STATE, B2_LAST_PRESSED, B3_LAST_STATE, B3_LAST_PRESSED
    now = pytz.timezone("Europe/Helsinki").localize(dt.datetime.now(), is_dst=None)
    msg = json.loads(payload)
    try:
        if msg.get('mac') == '80:7D:3A:47:59:BA':
            values = msg.get('data')
            b2 = values.get('b2')
            b3 = values.get('b3')
            if b2 is not None and b2 != B2_LAST_STATE:
                now_str = now.strftime('%d.%m. klo %H:%M:%S')
                if b2 == 1:
                    B2_LAST_PRESSED = now
                    response = send_telegram(f'Button pressed {now_str}', 1)
                B2_LAST_STATE = b2
            elif b3 is not None and b3 != B3_LAST_STATE:
                now_str = now.strftime('%d.%m. klo %H:%M:%S')
                if b3 == 1:
                    B3_LAST_PRESSED = now
                    response = send_telegram(f'Button pressed {now_str}', 2)
                B3_LAST_STATE = b3
    except Exception as err:
        print(err)


def send_telegram(msg, p):
    global PRESETS1, CURRENT_PRESET1, PRESETS2, CURRENT_PRESET2, SIMULATE, TIMER, AUTOCHANGE
    TIMER.cancel()
    if p == 1:
        CURRENT_PRESET1 += 1
        if CURRENT_PRESET1 >= len(PRESETS1):
            CURRENT_PRESET1 = 0
        data = PRESETS1[CURRENT_PRESET1]
        logging.info(f"Preset 1: #{CURRENT_PRESET1}")
    else:
        CURRENT_PRESET2 += 1
        if CURRENT_PRESET2 >= len(PRESETS2):
            CURRENT_PRESET2 = 0
        data = PRESETS2[CURRENT_PRESET2]
        logging.info(f"Preset 2: #{CURRENT_PRESET2}")
    headers = {"Content-Type": "application/json"}
    for ip in WLED_IPS:
        url = f'http://{ip}/json'
        if SIMULATE is False:
            res = requests.post(url, data=data, headers=headers, timeout=1)
            print(f"{url} {res.status_code}")
        else:
            print(f"{url} (simulated)")
    TIMER = threading.Timer(AUTOCHANGE, next_effect)
    TIMER.start()


def get_setting(args, arg, config, section, key, envname, default=None):
    # Return command line argument, if it exists
    if args and hasattr(args, arg) and getattr(args, arg) is not None:
        return getattr(args, arg)
    # Return value from config.ini if it exists
    elif section and key and section in config and key in config[section]:
        return config[section][key]
    # Return value from env if it exists
    elif envname:
        return os.environ.get(envname)
    else:
        return default


def main():
    global SIMULATE, TIMER, AUTOCHANGE
    args = get_args()
    AUTOCHANGE = args.autochange
    if args.simulate:
        SIMULATE = True
    config = configparser.ConfigParser()
    dir_path = os.path.dirname(os.path.realpath(__file__))
    config.read(os.path.join(dir_path, args.config))
    mqtt_user = get_setting(args, 'mqtt_username', config, 'mqtt', 'username', 'MQTT_USERNAME', default='')
    mqtt_pass = get_setting(args, 'mqtt_password', config, 'mqtt', 'password', 'MQTT_PASSWORD', default='')
    mqtt_host = get_setting(args, 'mqtt_host', config, 'mqtt', 'host', 'MQTT_HOST', default='127.0.0.1')
    mqtt_port = get_setting(args, 'mqtt_port', config, 'mqtt', 'port', 'MQTT_PORT', default='1883')
    # mqtt_topic = get_setting(args, 'mqtt_topic', config, 'mqtt', 'topic', 'MQTT_TOPIC', default='')
    load_presets(args)
    # Blocking call that processes network traffic, dispatches callbacks and
    # handles reconnecting.
    # Other loop*() functions are available that give a threaded interface and a
    # manual interface.
    mclient = mqtt.Client()
    mclient.args = args
    if mqtt_user != '':
        mclient.username_pw_set(mqtt_user, mqtt_pass)
        logging.debug(f'Using MQTT username and password')
    mclient.on_connect = on_connect
    mclient.on_message = on_message
    logging.info(f'Connecting to {mqtt_host}:{mqtt_port}')
    mclient.connect(mqtt_host, int(mqtt_port), 60)
    logging.info('Start listening topic(s): {}'.format(', '.join(args.topic)))
    try:
        TIMER = threading.Timer(AUTOCHANGE, next_effect)
        TIMER.start()
        mclient.loop_forever()
    except KeyboardInterrupt:
        mclient.disconnect()
        TIMER.cancel()
        if args.quiet is False:
            print("Good bye")


if __name__ == '__main__':
    main()
