"""Integration-test fixtures: a real ClickHouse server via testcontainers.

Modeled on detectkit's `tests/integration/conftest.py` — the whole module is
skipped when `testcontainers`/Docker is unavailable, so `pytest -m "not
integration"` (the default local/CI loop) never touches Docker.

Parametrized over two independent axes, both consumed by every test through
the `manager`/`test_db` fixtures:

- **server image** — `clickhouse_container` (`AFLY_CH_IMAGES`, comma
  separated, default both 22.11 and 26.3: the oldest server this project
  still targets, and the analyst's actual 26.3 warehouse). One container per
  image value, reused across both protocols tested against it.
- **transport** — `protocol` (`native` / `http`) — exercises
  `ClickHouseManager.from_profile`'s two client paths
  (`clickhouse_driver.Client` vs `afly.database._http_client.HttpClient`)
  against the exact same server, so a behavioural difference between them
  shows up as a protocol-specific test failure rather than being silently
  swallowed by only ever testing native.

`ClickHouseContainer` (from `testcontainers[clickhouse]`) already exposes
both the native port (9000) and the HTTP port (8123) on every container it
starts — see its `__init__`, which calls `with_exposed_ports` for both —
so no container-side change was needed to add the HTTP axis.
"""

from __future__ import annotations

import os
import socket
import uuid
from collections.abc import Iterator

import pytest

from afly.config.profile import ClickHouseProfile
from afly.database.clickhouse import ClickHouseManager

pytestmark = pytest.mark.integration

pytest.importorskip("testcontainers", reason="install the 'integration' extra to run these tests")

_DEFAULT_IMAGES = (
    "clickhouse/clickhouse-server:22.11",
    "clickhouse/clickhouse-server:26.3",
)
_HTTP_PORT = 8123


def _images() -> tuple[str, ...]:
    """The server image(s) to run the suite against — see the module docstring."""
    raw = os.environ.get("AFLY_CH_IMAGES")
    if not raw:
        return _DEFAULT_IMAGES
    images = tuple(s.strip() for s in raw.split(",") if s.strip())
    return images or _DEFAULT_IMAGES


def _image_id(image: str) -> str:
    """Short pytest id for an image param — the tag, not the full `repo:tag`."""
    return image.rsplit(":", 1)[-1]


def _docker_available() -> bool:
    """Cheap probe: does the local docker socket accept connections?"""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            sock.connect("/var/run/docker.sock")
            return True
    except OSError:
        return False


if not _docker_available():  # pragma: no cover - environment dependent
    pytest.skip(
        "Docker daemon not reachable; skipping ClickHouse integration suite",
        allow_module_level=True,
    )


@pytest.fixture(scope="session", params=_images(), ids=_image_id)
def clickhouse_container(request) -> Iterator[object]:  # type: ignore[no-untyped-def]
    cc = pytest.importorskip("testcontainers.community.clickhouse")
    container = cc.ClickHouseContainer(request.param)
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="session", params=["native", "http"])
def protocol(request) -> str:  # type: ignore[no-untyped-def]
    return str(request.param)


def profile_for(
    container, protocol_name: str, database: str  # type: ignore[no-untyped-def]
) -> ClickHouseProfile:
    """Build a `ClickHouseProfile` for *container* over *protocol_name* — shared by `manager` and any test that needs its own profile (e.g. the deep-checks test)."""
    port = container.port if protocol_name == "native" else _HTTP_PORT
    return ClickHouseProfile(
        host=container.get_container_host_ip(),
        port=int(container.get_exposed_port(port)),
        protocol=protocol_name,  # type: ignore[arg-type]
        user=container.username,
        password=container.password,
        database=database,
    )


@pytest.fixture(scope="session")
def manager(clickhouse_container, protocol) -> Iterator[ClickHouseManager]:  # type: ignore[no-untyped-def]
    """A `ClickHouseManager` wrapping a real client on `protocol`, against `clickhouse_container`."""
    profile = profile_for(clickhouse_container, protocol, clickhouse_container.dbname)
    mgr = ClickHouseManager.from_profile(profile)
    try:
        yield mgr
    finally:
        mgr.close()


@pytest.fixture()
def test_db(manager: ClickHouseManager) -> Iterator[str]:
    """A freshly created, uniquely-named database — dropped after the test.

    Each test gets its own database so tests can run in any order against
    the one session-shared container without colliding on table names.
    """
    db = f"afly_it_{uuid.uuid4().hex[:12]}"
    manager.create_database(db)
    try:
        yield db
    finally:
        manager.execute(f"DROP DATABASE IF EXISTS `{db}`")
