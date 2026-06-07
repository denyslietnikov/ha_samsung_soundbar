import base64
import logging
from datetime import datetime

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo

from .api_extension.SoundbarDevice import SoundbarDevice
from .const import CONF_ENTRY_DEVICE_ID, DOMAIN
from .models import DeviceConfig

_LOGGER = logging.getLogger(__name__)

_TRANSPARENT_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
    "/x8AAwMB/6X8XMsAAAAASUVORK5CYII="
)


async def async_setup_entry(hass, config_entry, async_add_entities):
    domain_data = hass.data[DOMAIN]

    entities = []
    for key in domain_data.devices:
        device_config: DeviceConfig = domain_data.devices[key]
        device = device_config.device
        if device.device_id == config_entry.data.get(CONF_ENTRY_DEVICE_ID):
            entities.append(SoundbarImageEntity(device, "Image URL", hass))
    async_add_entities(entities)
    return True


class SoundbarImageEntity(ImageEntity):
    def __init__(
        self, device: SoundbarDevice, append_unique_id: str, hass: HomeAssistant
    ):
        super().__init__(hass)

        self.__device = device
        self._attr_unique_id = f"{device.device_id}_sw_{append_unique_id}"
        self._attr_name = "Media Artwork"
        self._attr_entity_registry_enabled_default = False
        self._attr_should_poll = True
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self.__device.device_id)},
            name=self.__device.device_name,
            manufacturer=self.__device.manufacturer,
            model=self.__device.model,
            sw_version=self.__device.firmware_version,
        )

        self.__updated = None
        self.__using_placeholder = False

    # ---------- GENERAL ---------------
    @property
    def entity_picture(self) -> str | None:
        """Return a local proxy URL for real artwork or the no-artwork placeholder."""
        self.__sync_image_state()
        return super().entity_picture

    @property
    def image_url(self) -> str | None:
        """Return URL of image."""
        return self.__device.media_coverart_url or None

    @property
    def content_type(self) -> str | None:
        """Return content type for the generated placeholder image."""
        if self.image_url is None:
            return "image/png"
        return super().content_type

    @property
    def image_last_updated(self) -> datetime | None:
        """The time when the image was last updated."""
        self.__sync_image_state()
        return self.__updated

    async def async_update(self) -> None:
        """Refresh image entity state from the shared soundbar device cache."""
        self.__sync_image_state()

    async def async_image(self) -> bytes | None:
        """Return current artwork bytes, or a valid placeholder when idle."""
        if self.image_url is None:
            self.__sync_image_state()
            return _TRANSPARENT_PNG
        return await super().async_image()

    def __sync_image_state(self) -> None:
        """Clear cached bytes when the soundbar artwork changes."""
        current = self.__device.media_coverart_updated
        if current is None:
            if not self.__using_placeholder:
                self._cached_image = None
                self.__updated = datetime.now()
                self.__using_placeholder = True
            return

        if self.__updated != current or self.__using_placeholder:
            self._cached_image = None
            self.__updated = current
            self.__using_placeholder = False

    @property
    def name(self):
        return self._attr_name
