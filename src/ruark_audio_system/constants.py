META_INFO = """<?xml version="1.0" encoding="utf-8"?>
<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/"
           xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"
           xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">
  <item id="0" parentID="-1" restricted="false">
    <dc:title>Internet Radio</dc:title>
    <res protocolInfo="http-get:*:audio/mpeg:*"
         duration="24:00:00.000">{url}</res>
    <upnp:class>object.item.audioItem.audioBroadcast</upnp:class>
    <upnp:radioCallSign>DLNA Radio</upnp:radioCallSign>
    <upnp:radioStationID>123456</upnp:radioStationID>
    <upnp:radioBand>Internet</upnp:radioBand>
    <upnp:channelNr>1</upnp:channelNr>
  </item>
</DIDL-Lite>"""
