import time
from abc import ABC, abstractmethod

from libetrv.bluetooth import btle
from loguru import logger

from etrv2mqtt.config import Config, ThermostatConfig
from etrv2mqtt.etrvutils import eTRVUtils
from etrv2mqtt.mqtt import Mqtt
from typing import Type, Dict, NoReturn
import schedule
from pprint import pprint


class DeviceBase(ABC):
    def __init__(self, thermostat_config: ThermostatConfig, config: Config):
        super().__init__()

    @abstractmethod
    def poll(self, mqtt: Mqtt):
        pass

    @abstractmethod
    def set_temperature(self, mqtt: Mqtt, temperature: float):
        pass


class TRVDevice(DeviceBase):
    def __init__(self, thermostat_config: ThermostatConfig, config: Config):
        super().__init__(thermostat_config, config)
        self._device = eTRVUtils.create_device(thermostat_config.address,
                                               bytes.fromhex(
                                                   thermostat_config.secret_key),
                                               retry_limit=config.retry_limit)
        self._name = thermostat_config.topic
        self._stay_connected = config.stay_connected
        self._is_polling = False

    def poll(self, mqtt: Mqtt):
        try:
            self._is_polling = True
            logger.debug("Polling data from {}", self._name)
             
            #publish message that data is being polled from this device
            mqtt.publish_device_data(self._name, str('{"status": "Atnaujinama"}'), True)

            if not self._device.is_connected():
                self._device.connect()

            ret = eTRVUtils.read_device(self._device)
            logger.debug(str(ret))
            mqtt.publish_device_data(self._name, str(ret), False)

            #publish message that data has being polled from this device
            self._is_polling = False
            mqtt.publish_device_data(self._name, str('{"status": "Atnaujinta"}'), True)
            if self._stay_connected == False:
                self._device.disconnect()
        except btle.BTLEDisconnectError as e:
            self._is_polling = False
            mqtt.publish_device_data(self._name, str('{"status": "Ryšio klaida"}'), True)
            logger.error(e)

    def set_temperature(self, mqtt: Mqtt, temperature: float):
        try:
            logger.info("Setting {} to {}C", self._name, temperature)

            if not self._device.is_connected():
                self._device.connect()
            eTRVUtils.set_temperature(self._device, temperature)
            # Home assistant needs to see updated temperature value to confirm change
            self.poll(mqtt)
        except btle.BTLEDisconnectError as e:
            logger.error(e)


class DeviceManager():
    def __init__(self, config: Config, deviceClass: Type[DeviceBase]):
        self._config = config
        self._devices: Dict[str, DeviceBase] = {}
        for thermostat_config in self._config.thermostats.values():
            logger.info("Adding device {} MAC: {} key: {}", thermostat_config.topic,
                        thermostat_config.address, thermostat_config.secret_key)
            device = deviceClass(thermostat_config, config)
            self._devices[thermostat_config.topic] = device

        self._mqtt = Mqtt(self._config)
        self._mqtt.set_temperature_callback = self._set_temperature_callback
        self._mqtt.hass_birth_callback = self._hass_birth_callback
        self._mqtt.poll_device_callback = self._poll_device_callback

    def _poll_devices(self):
        self._mqtt.is_polling = True
        for device in self._devices.values():
            device.poll(self._mqtt)
        self._mqtt.is_polling = False

        #cancel current job for poll_forever and create new one so polling interval will be counting from now on
        schedule.clear("poll_forever")
        schedule.every(self._config.poll_interval).seconds.do(
            self._poll_devices).tag("poll_forever")

    def poll_forever(self) -> NoReturn:
        schedule.every(self._config.poll_interval).seconds.do(
            self._poll_devices).tag("poll_forever")
        mqtt_was_connected: bool = False

        while True:
            if self._mqtt.is_connected():
                # run all pending jobs on connect
                if not mqtt_was_connected:
                    mqtt_was_connected = True
                    schedule.run_all(delay_seconds=1)

                schedule.run_pending()
                time.sleep(1)
            else:
                mqtt_was_connected = False
                time.sleep(2)

    def _set_temperature_task(self, device: DeviceBase, temperature: float):
        device.set_temperature(self._mqtt, temperature)
        # this will cause the task to be executed only once
        return schedule.CancelJob

    def _set_temperature_callback(self, mqtt: Mqtt, name: str, temperature: float):
        if name not in self._devices.keys():
            logger.warning(
                "Device {} not found", name)
            return
        device = self._devices[name]

        # cancel pending temeperature update for the same device
        schedule.clear(device)

        # schedule temeperature update
        schedule.every(self._config.setpoint_debounce_time).seconds.do(
            self._set_temperature_task, device, temperature).tag(device)

    def _hass_birth_callback(self, mqtt: Mqtt):
        schedule.run_all(delay_seconds=1)

    def _poll_device_callback(self, mqtt: Mqtt, name: str):
        if name != 'all':
            if name not in self._devices.keys():
                logger.warning(
                    "Device {} not found", name)
                return
            device = self._devices[name]
            if device._is_polling:
                logger.warning("Already polling " + name)
                return
            schedule.clear("poll_" + name)
            schedule.every().second.do(
                self._poll_device_task, device).tag("poll_" + name)
        else:
            if mqtt.is_polling:
                logger.warning("Already polling all devices")
                return
            schedule.clear("poll")
            schedule.every().second.do(
                self._poll_all_devices_task).tag("poll")            
            
    def _poll_device_task(self, device: DeviceBase):

        device.poll(self._mqtt)
        
        # this will cause the task to be executed only once
        return schedule.CancelJob
    
    def _poll_all_devices_task(self):

        self._poll_devices()
        
        # this will cause the task to be executed only once
        return schedule.CancelJob 
