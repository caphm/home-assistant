"""Support for TVs through the SmartThings cloud API."""
from typing import Optional, Sequence

from pysmartthings import Attribute, Capability
from pysmartthings.capability import ATTRIBUTE_ON_VALUES

from homeassistant.components.media_player import (MediaPlayerDevice, SUPPORT_NEXT_TRACK,
    SUPPORT_PAUSE, SUPPORT_PLAY, SUPPORT_PLAY_MEDIA, SUPPORT_PREVIOUS_TRACK,
    SUPPORT_SELECT_SOUND_MODE, SUPPORT_SELECT_SOURCE, SUPPORT_STOP, SUPPORT_TURN_OFF,
    SUPPORT_TURN_ON, SUPPORT_VOLUME_MUTE, SUPPORT_VOLUME_SET, SUPPORT_VOLUME_STEP)
from homeassistant.const import (STATE_ON, STATE_OFF, STATE_IDLE, STATE_PLAYING, STATE_PAUSED)

from . import SmartThingsEntity
from .const import DATA_BROKERS, DOMAIN

SUPPORTED_FEATURES = (
    SUPPORT_NEXT_TRACK | SUPPORT_PAUSE | SUPPORT_PLAY | SUPPORT_PLAY_MEDIA |
    SUPPORT_PREVIOUS_TRACK | SUPPORT_SELECT_SOUND_MODE | SUPPORT_SELECT_SOURCE |
    SUPPORT_STOP | SUPPORT_TURN_OFF | SUPPORT_TURN_ON | SUPPORT_VOLUME_MUTE |
    SUPPORT_VOLUME_SET | SUPPORT_VOLUME_STEP
)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Platform uses config entry setup."""
    pass


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Add switches for a config entry."""
    broker = hass.data[DOMAIN][DATA_BROKERS][config_entry.entry_id]
    async_add_entities(
        [
            SmartThingsTV(device)
            for device in broker.devices.values()
            if broker.any_assigned(device.device_id, "switch")
                and device.device_type_name == "Samsung OCF TV"
        ]
    )


def get_capabilities(capabilities: Sequence[str]) -> Optional[Sequence[str]]:
    """Return all capabilities supported if minimum required are present."""
    required = [Capability.switch, Capability.tv_channel, Capability.media_input_source, Capability.media_playback, Capability.audio_volume, Capability.audio_mute]
    supported = required + [Capability.ocf, Capability.execute, "custom.launchapp"]
    if all(capability in capabilities for capability in required):
        return supported


class SmartThingsTV(SmartThingsEntity, MediaPlayerDevice):
    """Define a SmartThings TV."""

    async def async_update(self):
        """Retrieve latest state."""
        await self._device.status.refresh()

    @property
    def supported_features(self):
        """Flag media player features that are supported."""
        return SUPPORTED_FEATURES

    @property
    def should_poll(self):
        """Enable polling because push events aren't that reliable."""
        return True

    @property
    def state(self):
        """State of the player."""
        if self._device.status.switch:
            playback_status = self._device.status.attributes[Attribute.playback_status]
            if playback_status in ["play", "fast forward", "rewind"]:
                return STATE_PLAYING
            elif playback_status == "pause":
                return STATE_PAUSED
            elif playback_status == "stop":
                return STATE_IDLE

            return STATE_ON

        return STATE_OFF

    @property
    def volume_level(self):
        """Volume level of the media player (0..1)."""
        return self._device.status.attributes[Attribute.volume].value / 100

    @property
    def is_volume_muted(self):
        """Boolean if volume is currently muted."""
        return self._device.status.attributes[Attribute.mute].value == "mute"

    @property
    def source_list(self):
        """List of available input sources."""
        return self._device.status.attributes[Attribute.supported_input_sources].value

    @property
    def source(self):
        """Name of the current input source."""
        return self._device.status.attributes[Attribute.input_source].value

    # @property
    # def media_title(self):
    #     """Title of current playing media."""
    #     if self.app_name:
    #         return self.app_name
    #     elif self.media_channel_name:
    #         return self.media_channel_name
    #     return None

    @property
    def media_channel(self):
        """Channel currently playing."""
        channel = self._device.status.attributes[Attribute.tv_channel].value
        return channel if channel else None

    @property
    def media_channel_name(self):
        """Name of the channel currently playing."""
        channel_name = self._device.status.attributes["tvChannelName"].value
        return channel_name if channel_name and not self.app_id else None

    @property
    def app_id(self):
        """ID of the current running app."""
        raw_name = self._device.status.attributes["tvChannelName"].value
        if raw_name and "." in raw_name:
            return raw_name
        return None

    @property
    def app_name(self):
        """Name of the current running app."""
        raw_name = self._device.status.attributes["tvChannelName"].value
        if raw_name:
            parts = raw_name.split(".")
            if len(parts) > 1:
                return parts[1]
        return None

    async def async_turn_off(self, **kwargs) -> None:
        """Turn the TV off."""
        await self._device.switch_off()
        self.async_schedule_update_ha_state(True)

    async def async_turn_on(self, **kwargs) -> None:
        """Turn the TV on."""
        await self._device.switch_on()
        self.async_schedule_update_ha_state(True)

    async def async_set_volume_level(self, volume):
        """Set volume level, range 0..1."""
        await self._device.command("main", Capability.audio_volume, "setVolume", [min(100, max(0, int(volume * 100)))])
        self.async_schedule_update_ha_state(True)

    async def async_volume_up(self):
        """Turn volume up for media player."""
        await self._device.command("main", Capability.audio_volume, "volumeUp")
        self.async_schedule_update_ha_state(True)

    async def async_volume_down(self):
        """Turn volume down for media player."""
        await self._device.command("main", Capability.audio_volume, "volumeDown")
        self.async_schedule_update_ha_state(True)

    async def async_mute_volume(self, mute):
        """Mute the volume."""
        await self._device.command("main", Capability.audio_mute, "mute" if mute else "unmute")
        self.async_schedule_update_ha_state(True)

    async def async_media_play(self):
        """Send play command."""
        await self._device.command("main", Capability.media_playback, "play")
        self.async_schedule_update_ha_state(True)

    async def async_media_pause(self):
        """Send pause command."""
        await self._device.command("main", Capability.media_playback, "pause")
        self.async_schedule_update_ha_state(True)

    async def async_media_stop(self):
        """Send stop command."""
        await self._device.command("main", Capability.media_playback, "stop")
        self.async_schedule_update_ha_state(True)

    async def async_media_previous_track(self):
        """Send previous track command."""
        await self._device.command("main", Capability.media_playback, "previous")
        self.async_schedule_update_ha_state(True)

    async def async_media_next_track(self):
        """Send previous track command."""
        await self._device.command("main", Capability.media_playback, "next")
        self.async_schedule_update_ha_state(True)

    async def async_select_source(self, source):
        """Select input source."""
        await self._device.command("main", Capability.media_input_source, "setInputSource", [source])
        self.async_schedule_update_ha_state(True)
