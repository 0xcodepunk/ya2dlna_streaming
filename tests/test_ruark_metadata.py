from ruark_audio_system.ruark_r5_controller import RuarkR5Controller


def test_metadata_contains_artist_and_title():
    controller = RuarkR5Controller()

    metadata = controller.generate_metadata_with_fake_duration(
        "http://host/live_stream.mp3",
        title="Песня",
        artist="Артист",
    )

    assert "<dc:title>Артист — Песня</dc:title>" in metadata
    assert "<upnp:artist>Артист</upnp:artist>" in metadata


def test_metadata_escapes_xml_special_characters():
    controller = RuarkR5Controller()

    metadata = controller.generate_metadata_with_fake_duration(
        "http://host/live_stream.mp3",
        title="R&B <Mix>",
        artist="A&B",
    )

    assert "A&amp;B — R&amp;B &lt;Mix&gt;" in metadata
    assert "<Mix>" not in metadata


def test_metadata_without_artist_uses_plain_title():
    controller = RuarkR5Controller()

    metadata = controller.generate_metadata_with_fake_duration(
        "http://host/live_stream.mp3",
        title="Радио Jazz",
    )

    assert "<dc:title>Радио Jazz</dc:title>" in metadata


def test_metadata_defaults_to_internet_radio():
    controller = RuarkR5Controller()

    metadata = controller.generate_metadata_with_fake_duration(
        "http://host/live_stream.mp3"
    )

    assert "<dc:title>Internet Radio</dc:title>" in metadata
