"""Fake Azure IoT Hub MQTT/TLS broker for the Aldes Connect Box.

Launcher kept for compatibility (`python fake_iothub.py`, Docker CMD): the code lives
in the aldes_bridge package. Configuration (env vars): see aldes_bridge/config.py.
"""
from aldes_bridge.app import main

if __name__ == "__main__":
    main()
