import enum
from logging import getLogger

logger = getLogger(__name__)


class RadioStations(enum.Enum):
    """Поддерживаемые радиостанции"""
    rock_fm = "http://nashe1.hostingradio.ru:80/rock-256"  # Rock FM 95.2
    jazz = "http://nashe1.hostingradio.ru:80/jazz-256"  # Jazz FM 89.1
    nashe = "http://nashe1.hostingradio.ru:80/nashe-256"  # Nashe FM 101.8
    ultra = "http://nashe1.hostingradio.ru:80/ultra-256"  # Ultra FM 100.5
    energy = "https://srv11.gpmradio.ru:8443/stream/air/aac/64/99"  # Energy FM 104.2  # noqa: E501
    retro_fm = "https://retro.hostingradio.ru:8043/retro256.mp3"  # Retro FM 88.3  # noqa: E501
    studio21 = "https://stream.studio21.ru/studio2196.aacp"  # Studio 21 93.2  # noqa: E501
    relax_fm = "https://srv11.gpmradio.ru:8443/stream/air/aac/64/200"  # Relax FM 90.8  # noqa: E501
    monte_carlo = "https://montecarlo.hostingradio.ru/montecarlo96.aacp"  # Monte Carlo 102.1  # noqa: E501
    tihiy_don = "http://185.154.72.15:8000"  # Тихий Дон 73.76
    taxi_fm = "https://stream2.n340.com/13_taxi_64?type=.aac&UID=0BB099281DC03DD138A95B51F428908F"  # Такси FM 96.4  # noqa: E501
    radio7 = "https://radio7.hostingradio.ru:8040/radio7256.mp3"  # Radio 7 103.8  # noqa: E501
    avtoradio = "https://srv01.gpmradio.ru/stream/air/aac/64/100"  # Авторадио 90.3  # noqa: E501
    radio_dfm = "https://dfm.hostingradio.ru/dfm96.aacp"  # DFM 101.2  # noqa: E501
    like_fm = "https://srv11.gpmradio.ru:8443/stream/air/aac/64/219"  # Like FM 87.9  # noqa: E501
    vostok_fm = "https://stream2.n340.com:8443/18_vostok_64_reg_1?type=.aac&UID=0BB099281DC03DD138A95B51F428908F"  # Восток FM 94.0  # noqa: E501
    zvezda = "https://zvezda-radio-rzv.mediacdn.ru/radio/zvezda/zvezda_128"  # Звезда 95.6  # noqa: E501
    chanson = "https://chanson.hostingradio.ru:8041/chanson256.mp3"  # Шансон 103.0  # noqa: E501
    vostok_rossii = "https://radiovostok.hopto.org/"  # Восток России 103.7
    jam_fm = "https://online.jamfm.ru/jam?v=1744473935697"  # Jam FM 102.5
    europa_plus = "https://ep256.hostingradio.ru:8052/europaplus256.mp3"  # Европа+ 106.2  # noqa: E501
    dorognoe_radio = "https://dorognoe.hostingradio.ru/dorognoe"  # Дорожное радио 96.0  # noqa: E501
    zhara = "https://hls-01-zharafm.hostingradio.ru/zharafm/112/playlist.m3u8"  # Жара FM 100.5  # noqa: E501
    rock_arsenal = "https://online.rockarsenal.ru/rockwotalk?v=1744474589441"  # Rock Arsenal 104.5  # noqa: E501
    hit_fm = "https://hitfm.hostingradio.ru/hitfm96.aacp"  # Hit FM 107.4  # noqa: E501
    capital_fm = "https://icecast-vgtrk.cdnvideo.ru/capitalfmmp3"  # Capital FM 105.3  # noqa: E501
    remix_fm = "https://rmx.amgradio.ru/RemixFM"  # Remix FM 104.8  # noqa: E501
    comedy_radio = "https://srv21.gpmradio.ru:8443/stream/air/aac/64/202"  # Comedy Radio 102.5  # noqa: E501


async def get_radio_stations(station_name: str):
    """Получение URL радиостанции"""
    if hasattr(RadioStations, station_name):
        stream_url = RadioStations[station_name].value
        return stream_url
    else:
        logger.error(f"❌ Неизвестная радиостанция: {station_name}")
        return None
