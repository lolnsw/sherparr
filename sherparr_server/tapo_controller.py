import os
from datetime import datetime
from PyP100 import PyP110
from shared.models import PowerStatus


class TapoController:
    def __init__(self):
        self.device = None
        self.ip = os.getenv("TAPO_IP", "192.168.1.100")
        self.username = os.getenv("TAPO_USERNAME")
        self.password = os.getenv("TAPO_PASSWORD")
        
        if not self.username or not self.password:
            raise ValueError("TAPO_USERNAME and TAPO_PASSWORD environment variables required")

    async def initialize(self):
        self.device = PyP110.P110(self.ip, self.username, self.password)
        await self._handshake()

    async def _handshake(self):
        try:
            self.device.handshake()
            self.device.login()
        except Exception as e:
            raise Exception(f"Failed to connect to Tapo device: {e}")

    async def turn_on(self):
        try:
            self.device.turnOn()
        except Exception as e:
            await self._handshake()
            self.device.turnOn()

    async def turn_off(self):
        try:
            self.device.turnOff()
        except Exception as e:
            await self._handshake()
            self.device.turnOff()

    async def get_status(self) -> PowerStatus:
        try:
            info = self.device.getDeviceInfo()
            energy = self.device.getEnergyUsage()
            
            return PowerStatus(
                is_on=info["device_on"],
                power_consumption=energy.get("current_power", 0) / 1000,  # Convert to watts
                last_update=datetime.now()
            )
        except Exception as e:
            await self._handshake()
            info = self.device.getDeviceInfo()
            energy = self.device.getEnergyUsage()
            
            return PowerStatus(
                is_on=info["device_on"],
                power_consumption=energy.get("current_power", 0) / 1000,
                last_update=datetime.now()
            )

    async def close(self):
        pass