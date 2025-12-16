"""Module for color_palette_extractor (RGB extraction from images) component."""

import asyncio
import io
import logging

import aiohttp
from colorthief import ColorThief
from PIL import UnidentifiedImageError
import voluptuous as vol

from homeassistant.components.light import (
    ATTR_RGB_COLOR,
    DOMAIN as LIGHT_DOMAIN,
    LIGHT_TURN_ON_SCHEMA,
)
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_ON as LIGHT_SERVICE_TURN_ON
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import aiohttp_client, config_validation as cv

from .const import ATTR_PATH, ATTR_URL, DOMAIN, SERVICE_TURN_ON

_LOGGER = logging.getLogger(__name__)

# Extend the existing light.turn_on service schema
SERVICE_SCHEMA = vol.All(
    cv.has_at_least_one_key(ATTR_URL, ATTR_PATH),
    cv.make_entity_service_schema(
        {
            **LIGHT_TURN_ON_SCHEMA,
            vol.Exclusive(ATTR_PATH, "color_palette_extractor"): cv.isfile,
            vol.Exclusive(ATTR_URL, "color_palette_extractor"): cv.url,
        }
    ),
)


def _get_file(file_path: str) -> str:
    """Get a PIL acceptable input file reference.

    Allows us to mock patch during testing to make BytesIO stream.
    """
    return file_path


def _get_color(file_handler: io.BytesIO | str, light_count: int = 1) -> list[tuple[int, int, int]]:
    """Given an image file, extract the predominant color from it."""
    color_thief = ColorThief(file_handler)

    if light_count == 1:
        # get_color returns a single RGB value for the given image
        colors = [color_thief.get_color(quality=1)]
        _LOGGER.debug("get_palette response: %s", colors)
    else:
        colors = color_thief.get_palette(quality=1, color_count=light_count)
        _LOGGER.debug("get_palette response: %s", colors)

    _LOGGER.debug("Extracted %d RGB colors from image", len(colors))

    return colors


async def _async_extract_color_from_url(
    hass: HomeAssistant, url: str, number_of_lights: int = 1
) -> tuple[int, int, int] | None:
    """Handle call for URL based image."""
    if not hass.config.is_allowed_external_url(url):
        _LOGGER.error(
            (
                "External URL '%s' is not allowed, please add to"
                " 'allowlist_external_urls'"
            ),
            url,
        )
        return None

    _LOGGER.debug("Getting predominant RGB from image URL '%s'", url)

    # Download the image into a buffer for ColorThief to check against
    try:
        session = aiohttp_client.async_get_clientsession(hass)

        async with asyncio.timeout(10):
            response = await session.get(url)

    except (TimeoutError, aiohttp.ClientError) as err:
        _LOGGER.error("Failed to get ColorThief image due to HTTPError: %s", err)
        return None

    content = await response.content.read()

    with io.BytesIO(content) as _file:
        _file.name = "color_palette_extractor.jpg"
        _file.seek(0)

        return _get_color(_file, number_of_lights)


def _extract_color_from_path(
    hass: HomeAssistant, file_path: str, number_of_lights: int = 1
) -> tuple[int, int, int] | None:
    """Handle call for local file based image."""
    if not hass.config.is_allowed_path(file_path):
        _LOGGER.error(
            "File path '%s' is not allowed, please add to 'allowlist_external_dirs'",
            file_path,
        )
        return None

    _LOGGER.debug("Getting predominant RGB from file path '%s'", file_path)

    _file = _get_file(file_path)
    return _get_color(_file, number_of_lights)


async def async_handle_service(service_call: ServiceCall) -> None:
    """Decide which color_palette_extractor method to call based on service."""
    service_data = dict(service_call.data)
    number_of_lights = len(service_data[ATTR_ENTITY_ID])

    try:
        if ATTR_URL in service_data:
            image_type = "URL"
            image_reference = service_data.pop(ATTR_URL)
            colors = await _async_extract_color_from_url(
                service_call.hass, image_reference, number_of_lights
            )

        elif ATTR_PATH in service_data:
            image_type = "file path"
            image_reference = service_data.pop(ATTR_PATH)
            colors = await service_call.hass.async_add_executor_job(
                _extract_color_from_path,
                service_call.hass,
                image_reference,
                number_of_lights,
            )

    except UnidentifiedImageError as ex:
        _LOGGER.error(
            "Bad image from %s '%s' provided, are you sure it's an image? %s",
            image_type,
            image_reference,
            ex,
        )
        return

    if colors:
        if isinstance(service_data[ATTR_ENTITY_ID], list):
            lights = service_data[ATTR_ENTITY_ID]
        else:
            lights = [service_data[ATTR_ENTITY_ID]]

        for entity_id, color in zip(lights, colors):
            data = {
                **service_data,
                ATTR_ENTITY_ID: entity_id,
                ATTR_RGB_COLOR: color,
            }

            await hass.services.async_call(
                LIGHT_DOMAIN, LIGHT_SERVICE_TURN_ON, data, blocking=True
            )


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the services."""

    hass.services.async_register(
        DOMAIN,
        SERVICE_TURN_ON,
        async_handle_service,
        schema=SERVICE_SCHEMA,
    )
