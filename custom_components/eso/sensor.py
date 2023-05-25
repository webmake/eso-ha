from __future__ import annotations

from bs4 import BeautifulSoup
from collections import namedtuple
from datetime import timedelta, datetime
from functools import partial

from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.components.rest.data import RestData
from homeassistant.components.sensor import PLATFORM_SCHEMA
from homeassistant.const import (
    CONF_NAME,
    CONF_PASSWORD,
    CONF_SELECTOR,
    CONF_USERNAME,
    UnitOfEnergy,
)
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util
from random_user_agent.params import SoftwareName, OperatingSystem
from random_user_agent.user_agent import UserAgent

import functools
import json
import homeassistant.helpers.config_validation as cv
import logging
import pytz
import requests
import voluptuous as vol

software_names = [SoftwareName.CHROME.value]
operating_systems = [OperatingSystem.WINDOWS.value, OperatingSystem.LINUX.value]
user_agent_rotator = UserAgent(software_names = software_names, operating_systems = operating_systems, limit = 100)

_LOGGER = logging.getLogger(__name__)
_ENDPOINT_AUTH = 'https://mano.eso.lt/user/login'
_ENDPOINT_FILTERS = 'https://mano.eso.lt/consumption'
_ENDPOINT_REPORT = 'https://mano.eso.lt/consumption?ajax_form=1'

METHOD_GET = 'GET'
DEFAULT_ENCODING = 'UTF-8'
DEFAULT_NAME = 'ESO'
DEFAULT_VERIFY_SSL = True
DOMAIN = 'eso'

SCAN_INTERVAL = timedelta(minutes = 60)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_USERNAME): cv.string,
    vol.Required(CONF_PASSWORD): cv.string,
    vol.Required(CONF_SELECTOR): cv.string,
    vol.Optional(CONF_NAME, default = DEFAULT_NAME): cv.string,
})

cookie = None

async def async_setup_platform(hass, config, async_add_entities, discovery_info = None):
    """Set up the ESO sensor."""
    name = config.get(CONF_NAME)
    username = config.get(CONF_USERNAME)
    password = config.get(CONF_PASSWORD)
    objectName = config.get(CONF_SELECTOR)

    async def async_update_data():
        _LOGGER.debug('Getting data from https://mano.eso.lt/')

        global cookie
        if cookie is None:
            _LOGGER.debug('Token is empty, authenticating for the first time')
            cookie = await authAndGetToken(hass, username, password)

        await getRaw(hass, cookie, objectName)

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        # Name of the data. For logging purposes.
        name = DEFAULT_NAME,
        update_method = async_update_data,
        # Polling interval. Will only be polled if there are subscribers.
        update_interval = SCAN_INTERVAL,
    )

    coordinator.async_add_listener(lambda: async_add_entities([EsoSensorClass(coordinator)]))
    await coordinator.async_refresh()

    if not coordinator.last_update_success:
        _LOGGER.error('ESO initialization failed, fix error and restart ha')
        return False

async def authAndGetToken(hass, username, password):
    user_agent = user_agent_rotator.get_random_user_agent()
    headersAuth = {
        'User-Agent': user_agent,
        'Referer': _ENDPOINT_AUTH,
        'Connection': 'keep-alive',
        'X-Requested-With': 'XMLHttpRequest',
    }

    restInit = RestData(hass, METHOD_GET, _ENDPOINT_AUTH, DEFAULT_ENCODING, None, None, None, None, DEFAULT_VERIFY_SSL)

    await restInit.async_update()

    soup = BeautifulSoup(restInit.data, 'html.parser')
    login_form = soup.find('form', {'id': 'user-login-form'})
    login_data = {}
    for input_field in login_form.find_all('input'):
        login_data[input_field.get('name')] = input_field.get('value')

    # update the login data with credentials
    login_data.update({
        'name': username,
        'pass': password,
        'login_type': 1,
    })
    func = functools.partial(
        requests.post,
        _ENDPOINT_AUTH,
        headers = headersAuth,
        data = login_data,
    )

    restAuth = await hass.async_add_executor_job(func)

    if restAuth.status_code != 200:
        raise UpdateFailed(f"Error communicating with API: {restAuth}")
    else :
        _LOGGER.debug('Login successful' + str(restAuth.cookies))

    return restAuth.cookies


async def getRaw(hass, cookiesData, objectName):
    user_agent = user_agent_rotator.get_random_user_agent()
    headersData = {
        'User-Agent': user_agent,
        'Accept': 'application/json, text/plain, */*',
        'lang': 'en',
        'sec-ch-ua-platform': 'macOS',
        'Sec-Fetch-Site': 'same-origin',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Dest': 'empty',
        'Referer': _ENDPOINT_AUTH,
        'Accept-Language': 'en-US;q=0.9,en;q=0.8',
        'Connection': 'keep-alive',
        'X-Requested-With': 'XMLHttpRequest',
    }

    func = functools.partial(
        requests.post,
        _ENDPOINT_FILTERS,
        cookies = cookiesData,
        headers = headersData,
    )
    restForm = await hass.async_add_executor_job(func)

    soup = BeautifulSoup(restForm.content, 'html.parser')
    form = soup.find('form', id = 'eso-consumption-history-form')

    for select_tag in form.find_all('select'):
        if select_tag.find('option', text = lambda t: objectName in t):
            value = select_tag.find('option', text = lambda t: objectName in t)['value']

    # Define the form data
    consumption_data = {}
    form = soup.find('form', id = 'eso-consumption-history-form')
    for input in form.find_all('input'):
        if input.get('name'):
            consumption_data[input['name']] = input.get('value', '')

    for input in form.find_all('input', {'type': ['text', 'hidden']}):
        if input.get('name') and input.get('value'):
            consumption_data[input['name']] = input.get('value', '')

    _LOGGER.debug(consumption_data)

    now = datetime.now()
    consumption_data.update({
        '_triggering_element_name': 'op',
        '_wrapper_format': 'drupal_ajax',
        'objects[]': value,
        'display_type': 'hourly',
        'period': 'week',
        'next_button_value': now.strftime("%Y-%m-%d") + ' 00:00',
    })

    _LOGGER.debug(consumption_data)

    func = functools.partial(
        requests.post,
        _ENDPOINT_REPORT,
        cookies = cookiesData,
        headers = headersData,
        data = consumption_data,
    )
    restReport = await hass.async_add_executor_job(func)
    parsed_data = json.loads(restReport.content)
    for obj in parsed_data:
        if obj is not None and 'settings' in obj and obj['settings'] is not None and 'eso_consumption_history_form' in obj['settings']:
            eso_consumption_history_form = obj['settings']['eso_consumption_history_form']['graphics_data']['datasets']
            break

    if eso_consumption_history_form is None:
        _LOGGER.error('Unable to get Raw data from ESO')
        return False
    else :
        _LOGGER.debug('ESO Raw data fetched correctly ' + json.dumps(eso_consumption_history_form)[:1000] + ' ... ')
        for item in eso_consumption_history_form:
            statistics: list[StatisticData] = []
            label = item['label']
            sum = 0.0
            for record in item['record']:
                timezone = pytz.timezone('Europe/Vilnius')
                dt_naive = datetime.strptime(record.get('date', None), '%Y%m%d%H%M')
                dt_local = timezone.localize(dt_naive, is_dst = False)
                value = record.get('value', None)
                if value is not None:
                    sum += value
                statistics.append({
                    'start': dt_local.astimezone(pytz.UTC),
                    'state': value,
                    'sum': sum,
                })

            if label == 'Atiduota į tinklą':
                id = f"{DOMAIN}:eso_electricity_production"
            elif label == 'Gauta iš tinklo':
                id = f"{DOMAIN}:eso_electricity_consumption"
            elif label == 'Suprognozuotas pagal vidutinį suvartojimą':
                id = f"{DOMAIN}:eso_prediction_electricity_consumption"
            elif label == 'Suprognozuotas pagal vidutinę gamybą':
                id = f"{DOMAIN}:eso_prediction_electricity_production"
            else :
                id = f"{DOMAIN}:eso_electricity_new"
            metadata = {
                'source': DOMAIN,
                'name': label,
                'statistic_id': id,
                'unit_of_measurement': UnitOfEnergy.KILO_WATT_HOUR,
                'has_mean': False,
                'has_sum': True,
            }

            async_add_external_statistics(hass, metadata, statistics)

class EsoSensorClass(Entity):
    def __init__(self, coordinator):
        self._coordinator = coordinator

    @property
    def state(self):
        data = self._coordinator.data
        if data is None:
            return None
        return data.get('value')
