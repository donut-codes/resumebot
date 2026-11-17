"""The fake page of the smoke test must start however the OS lays out its ports."""

import tests.split.smoke_support as support
from tests.split.smoke_support import Fake


def test_a_fake_page_starts_when_no_os_chosen_tcp_port_has_a_free_udp_twin(monkeypatch):
    # Windows hands TCP ports out one after another and keeps blocks of UDP ports back (excluded ranges), so every
    # port the OS picks can lack a free UDP twin. The fake then takes a number below the dynamic range for both.
    real = support._udp_at

    def udp_at(port):
        if port not in support.FALLBACK_PORTS:
            raise PermissionError(10013, "an attempt was made to access a socket in a way forbidden")
        return real(port)

    monkeypatch.setattr(support, "_udp_at", udp_at)
    fake = Fake(page=True, version="9.9.9")
    try:
        assert fake.port in support.FALLBACK_PORTS
        assert fake._udp is not None and fake._udp.getsockname()[1] == fake.port
    finally:
        fake.stop()
