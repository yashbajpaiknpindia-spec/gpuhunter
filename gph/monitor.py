from __future__ import annotations

import re
import shlex
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


from .config import Config

UNITS = {
    "h/s": 1.0, "hs": 1.0,
    "kh/s": 1e3, "khs": 1e3,
    "mh/s": 1e6, "mhs": 1e6,
    "gh/s": 1e9, "ghs": 1e9,
    "th/s": 1e12, "ths": 1e12,
    "ph/s": 1e15, "phs": 1e15,
}


def parse_hashrate_hs(log_text: str, profile_regex: str | None = None) -> float:
    patterns: list[str] = []
    if profile_regex:
        patterns.append(profile_regex)
    patterns.extend([
        r"(?i)Total\s+([0-9]+(?:\.[0-9]+)?)\s*(H/s|KH/s|MH/s|GH/s|TH/s|PH/s)",
        r"(?i)Hashrate\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)\s*(H/s|KH/s|MH/s|GH/s|TH/s|PH/s)",
        r"(?i)([0-9]+(?:\.[0-9]+)?)\s*(H/s|KH/s|MH/s|GH/s|TH/s|PH/s)\s*(?:total|avg|average)",
    ])
    for pattern in patterns:
        try:
            matches = list(re.finditer(pattern, log_text))
        except re.error:
            continue
        if not matches:
            continue
        m = matches[-1]
        try:
            value = float(m.group(1))
            unit = str(m.group(2)).lower().replace(" ", "")
            return value * UNITS.get(unit, 1)
        except (IndexError, ValueError):
            continue
    return 0.0


def parse_share_metrics(log_text: str, accepted_regex: str | None = None) -> tuple[int, datetime | None]:
    regex = accepted_regex or r"(?i)\baccepted\b"
    try:
        matches = list(re.finditer(regex, log_text))
    except re.error:
        matches = []
    last_at = None
    if matches:
        # Miner logs commonly start with an ISO-like local timestamp.
        before = log_text[:matches[-1].start()].splitlines()[-1:] or [""]
        line = before[0]
        ts_match = re.search(r"(20\d\d-\d\d-\d\d[ T]\d\d:\d\d:\d\d)", line)
        if ts_match:
            try:
                last_at = datetime.strptime(ts_match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except ValueError:
                pass
    return len(matches), last_at


def parse_gpu_stats(output: str) -> tuple[float, float]:
    # nvidia-smi query output: utilization, temperature
    try:
        parts = output.strip().splitlines()[0].split(",")
        return float(parts[0].strip().replace("%", "")), float(parts[1].strip().replace("C", ""))
    except (IndexError, ValueError):
        return 0.0, 0.0


class RemoteMinerMonitor:
    def __init__(self, config: Config):
        self.config = config

    @staticmethod
    def connection_details(instance: dict[str, Any]) -> tuple[str, int] | None:
        host = instance.get("ssh_host") or instance.get("public_ipaddr") or instance.get("public_ip")
        port = instance.get("ssh_port") or instance.get("public_port")
        ports = instance.get("ports")
        if (not host or not port) and isinstance(ports, dict):
            for key, values in ports.items():
                if str(key).startswith("22/") and isinstance(values, list) and values:
                    entry = values[0]
                    host = host or entry.get("HostIp") or entry.get("host_ip")
                    port = port or entry.get("HostPort") or entry.get("host_port")
                    break
        try:
            p = int(port)
        except (ValueError, TypeError):
            return None
        if not host or p < 1:
            return None
        return str(host), p

    def read_metrics(self, instance: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
        try:
            import paramiko
        except ImportError as exc:
            raise RuntimeError("Paramiko is not installed; run pip install -r requirements.txt") from exc
        if not self.config.ssh_private_key_path:
            raise RuntimeError("SSH private key is not configured")
        detail = self.connection_details(instance)
        if not detail:
            raise RuntimeError("Vast instance response does not yet include a direct SSH host/port")
        host, port = detail
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            ssh.connect(
                hostname=host,
                port=port,
                username="root",
                key_filename=str(Path(self.config.ssh_private_key_path).expanduser()),
                timeout=self.config.ssh_connect_timeout_seconds,
                banner_timeout=self.config.ssh_connect_timeout_seconds,
                auth_timeout=self.config.ssh_connect_timeout_seconds,
                look_for_keys=False,
                allow_agent=False,
            )
            log_path = shlex.quote(str(profile.get("log_path") or "/tmp/gph-miner.log"))
            _, stdout, stderr = ssh.exec_command(f"tail -n 400 {log_path} 2>/dev/null", timeout=10)
            log_text = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")[:200]
            _, gpu_out, _ = ssh.exec_command("nvidia-smi --query-gpu=utilization.gpu,temperature.gpu --format=csv,noheader 2>/dev/null", timeout=8)
            stats_out = gpu_out.read().decode("utf-8", errors="replace")
            hashrate = parse_hashrate_hs(log_text, profile.get("hashrate_regex"))
            accepted, last_share = parse_share_metrics(log_text, profile.get("accepted_regex"))
            util, temp = parse_gpu_stats(stats_out)
            return {
                "hashrate_hs": hashrate,
                "accepted_share_lines": accepted,
                "last_share_at": last_share.isoformat() if last_share else None,
                "gpu_util": util,
                "gpu_temp": temp,
                "log_tail": log_text[-6000:],
                "monitor_error": err,
            }
        finally:
            ssh.close()
