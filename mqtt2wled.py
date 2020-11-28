import argparse
import datetime as dt
import glob
import json
import logging
import os
import threading

import configparser
import paho.mqtt.client as mqtt
import pytz
import requests


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-l",
        "--log",
        dest="log",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="ERROR",
        help="Set the logging level",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Never print a char (except on crash)",
    )
    parser.add_argument(
        "--autochange",
        type=float,
        default=300,
        help="Seconds to change to next default effect",
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="If set, do not send actual HTTP requests to leds",
    )
    parser.add_argument("--presets", help="Preset directory", required=True)
    parser.add_argument(
        "--config", help="Configuration file", default="config.ini", nargs="?"
    )
    parser.add_argument("-t", "--topic", required=True, nargs="+", help="MQTT topics")
    parser.add_argument("--mqtt_username", help="MQTT user name", nargs="?")
    parser.add_argument("--mqtt_password", help="MQTT password", nargs="?")
    parser.add_argument("--mqtt_host", help="MQTT host", nargs="?")
    parser.add_argument("--mqtt_port", help="MQTT port", nargs="?")
    args = parser.parse_args()
    if args.log:
        logging.basicConfig(
            format="%(asctime)s %(levelname)-8s %(message)s",
            level=getattr(logging, args.log),
        )
    return args


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


class WledController:
    def __init__(self):
        self.args = get_args()
        self.presets1 = []
        self.presets2 = []
        self.b2_last_state = -1
        self.b2_last_pressed = None
        self.b3_last_state = -1
        self.b3_last_pressed = None
        self.current_preset1 = 0
        self.current_preset2 = 0
        self.wled_ips = ["172.24.1.201", "172.24.1.202", "172.24.1.203", "172.24.1.204"]
        self.config = configparser.ConfigParser()
        dir_path = os.path.dirname(os.path.realpath(__file__))
        self.config.read(os.path.join(dir_path, self.args.config))
        self.mqtt_user = get_setting(
            self.args,
            "mqtt_username",
            self.config,
            "mqtt",
            "username",
            "MQTT_USERNAME",
            default="",
        )
        self.mqtt_pass = get_setting(
            self.args,
            "mqtt_password",
            self.config,
            "mqtt",
            "password",
            "MQTT_PASSWORD",
            default="",
        )
        self.mqtt_host = get_setting(
            self.args,
            "mqtt_host",
            self.config,
            "mqtt",
            "host",
            "MQTT_HOST",
            default="127.0.0.1",
        )
        self.mqtt_port = get_setting(
            self.args,
            "mqtt_port",
            self.config,
            "mqtt",
            "port",
            "MQTT_PORT",
            default="1883",
        )
        # mqtt_topic = get_setting(args, 'mqtt_topic', config, 'mqtt', 'topic', 'MQTT_TOPIC', default='')
        # Blocking call that processes network traffic, dispatches callbacks and
        # handles reconnecting.
        # Other loop*() functions are available that give a threaded interface and a
        # manual interface.
        self.mclient = mqtt.Client()
        self.mclient.args = self.args
        if self.mqtt_user != "":
            self.mclient.username_pw_set(self.mqtt_user, self.mqtt_pass)
            logging.debug(f"Using MQTT username and password")
        self.mclient.on_connect = self.on_connect
        self.mclient.on_message = self.on_message
        logging.info(f"Connecting to {self.mqtt_host}:{self.mqtt_port}")
        self.mclient.connect(self.mqtt_host, int(self.mqtt_port), 60)
        logging.info("Start listening topic(s): {}".format(", ".join(self.args.topic)))

        self.load_presets()
        self.timer = threading.Timer(self.args.autochange, self.next_effect)
        self.timer.start()

        try:
            self.mclient.loop_forever()
        except KeyboardInterrupt:
            self.mclient.disconnect()
            self.timer.cancel()
            if self.args.quiet is False:
                print("Good bye")

    def load_presets(self):
        preset_files = glob.glob(f"{self.args.presets}/preset-0*.json")
        for pf in preset_files:
            with open(pf, "rt") as f:
                self.presets1.append(f.read())
        preset_files = glob.glob(f"{self.args.presets}/preset-1*.json")
        for pf in preset_files:
            with open(pf, "rt") as f:
                self.presets2.append(f.read())
        logging.info(
            "Loaded {} presets #1 and {} presets #2".format(
                len(self.presets1), len(self.presets2)
            )
        )

    def on_connect(self, client, userdata, flags, rc):
        logging.info("Connected with result code {}".format(rc))
        # Subscribing in on_connect() means that if we lose the connection and
        # reconnect then subscriptions will be renewed.
        for t in client.args.topic:
            logging.info(f"Subscribe to {t}")
            client.subscribe(t)

    # The callback for when a PUBLISH message is received from the server.
    def on_message(self, client, userdata, msg):
        payload = msg.payload.decode("utf-8")
        if msg.retain == 1:
            logging.info("Do not handle retain message {}".format(payload))
            return
        logging.debug("{} '{}'".format(msg.topic, payload))
        self.handle_jsonsensor(client, userdata, msg, payload)

    def next_effect(self):
        self.send_telegram(1)

    def handle_jsonsensor(self, client, userdata, msg, payload):
        """
        {'sensor': 'button', 'mac': '80:7D:3A:47:59:BA', 'id': '200101', 'data': {'b1': 0, 'b2': 1, 'b3': 0}}
        """
        now = pytz.timezone("Europe/Helsinki").localize(dt.datetime.now(), is_dst=None)
        msg = json.loads(payload)
        try:
            if msg.get("mac") == "80:7D:3A:47:59:BA":
                values = msg.get("data")
                b2 = values.get("b2")
                b3 = values.get("b3")
                if b2 is not None and b2 != self.b2_last_state:
                    if b2 == 1:
                        self.b2_last_pressed = now
                        self.send_telegram(1)
                    self.b2_last_state = b2
                elif b3 is not None and b3 != self.b3_last_state:
                    if b3 == 1:
                        self.b3_last_pressed = now
                        self.send_telegram(2)
                    self.b3_last_state = b3
        except Exception as err:
            print(err)

    def send_telegram(self, p):
        self.timer.cancel()
        if p == 1:
            self.current_preset1 += 1
            if self.current_preset1 >= len(self.presets1):
                self.current_preset1 = 0
            data = self.presets1[self.current_preset1]
            logging.info(f"Preset 1: #{self.current_preset1}")
        else:
            self.current_preset2 += 1
            if self.current_preset2 >= len(self.presets2):
                self.current_preset2 = 0
            data = self.presets2[self.current_preset2]
            logging.info(f"Preset 2: #{self.current_preset2}")
        headers = {"Content-Type": "application/json"}
        for ip in self.wled_ips:
            url = f"http://{ip}/json"
            if self.args.simulate is False:
                res = requests.post(url, data=data, headers=headers, timeout=1)
                print(f"{url} {res.status_code}")
            else:
                print(f"{url} (simulated)")
        self.timer = threading.Timer(self.args.autochange, self.next_effect)
        self.timer.start()


def main():
    wc = WledController()


if __name__ == "__main__":
    main()
