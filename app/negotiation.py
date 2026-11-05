"""Capability negotiation with the calling agent ("platform").

Every UCP request carries a `UCP-Agent: profile="https://..."` header. The
store fetches that profile, intersects capabilities and only uses what both
sides support (spec: Negotiation Protocol / Intersection Algorithm).

Because this is a test store, the default mode is lenient: if the header is
missing or the profile cannot be fetched, the request still goes through with
all store capabilities and a note is logged. Set STRICT_NEGOTIATION=true to
get the spec's error responses instead.
"""

import ipaddress
import logging
import re
import socket
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from .config import Settings
from .profile import BUSINESS_CAPABILITIES, UCP_VERSION

log = logging.getLogger("ucp.negotiation")

_PROFILE_RE = re.compile(r'(?:^|[,;\s])profile\s*=\s*"([^"]*)"')
_CACHE_TTL = 600
_MAX_PROFILE_BYTES = 256 * 1024


class NegotiationError(Exception):
  def __init__(self, status: int, code: str, content: str):
    super().__init__(content)
    self.status = status
    self.code = code
    self.content = content


@dataclass
class Negotiated:
  active: set[str]
  profile_url: str | None
  note: str


_cache: dict[str, tuple[float, dict | NegotiationError]] = {}
_NEGATIVE_TTL = 60
_cache_lock = threading.Lock()


def parse_agent_header(value: str | None) -> str | None:
  if not value:
    return None
  match = _PROFILE_RE.search(value)
  return match.group(1) if match else None


def _check_url(url: str, settings: Settings) -> None:
  parsed = urlparse(url)
  if parsed.scheme not in ("https", "http") or not parsed.hostname:
    raise NegotiationError(400, "invalid_profile_url", "Profile URL is malformed.")
  if settings.allow_insecure_profile_urls:
    return
  if parsed.scheme != "https":
    raise NegotiationError(
      400, "invalid_profile_url", "Profile URL must use https."
    )
  # Do not let a caller make the store fetch internal addresses (SSRF).
  try:
    infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443)
  except OSError as exc:
    raise NegotiationError(
      400, "invalid_profile_url", "Profile host cannot be resolved."
    ) from exc
  for info in infos:
    ip = ipaddress.ip_address(info[4][0])
    if not ip.is_global:
      raise NegotiationError(
        400, "invalid_profile_url", "Profile URL points to a private address."
      )


def fetch_profile(url: str, settings: Settings) -> dict:
  now = time.time()
  with _cache_lock:
    hit = _cache.get(url)
  if hit:
    age, value = now - hit[0], hit[1]
    if isinstance(value, NegotiationError):
      if age < _NEGATIVE_TTL:
        raise value
    elif age < _CACHE_TTL:
      return value
  try:
    data = _fetch(url, settings)
  except NegotiationError as exc:
    with _cache_lock:
      _cache[url] = (now, exc)
    raise
  with _cache_lock:
    _cache[url] = (now, data)
  return data


def _fetch(url: str, settings: Settings) -> dict:
  _check_url(url, settings)
  try:
    with httpx.Client(timeout=3.0, follow_redirects=False) as client:
      resp = client.get(url, headers={"Accept": "application/json"})
  except httpx.HTTPError as exc:
    raise NegotiationError(
      424, "profile_unreachable", f"Profile fetch failed: {exc}"
    ) from exc
  if resp.status_code != 200:
    raise NegotiationError(
      424, "profile_unreachable", f"Profile returned HTTP {resp.status_code}."
    )
  if len(resp.content) > _MAX_PROFILE_BYTES:
    raise NegotiationError(422, "profile_malformed", "Profile is too large.")
  try:
    data = resp.json()
    if not isinstance(data, dict) or not isinstance(data.get("ucp"), dict):
      raise ValueError("missing ucp object")
  except ValueError as exc:
    raise NegotiationError(
      422, "profile_malformed", "Profile is not a valid UCP profile."
    ) from exc
  return data


def intersect(platform_profile: dict) -> set[str]:
  """Spec intersection algorithm: shared names, shared version, prune orphans."""
  platform_caps = platform_profile.get("ucp", {}).get("capabilities") or {}
  active: set[str] = set()
  for name in BUSINESS_CAPABILITIES:
    entries = platform_caps.get(name)
    if not isinstance(entries, list):
      continue
    versions = {e.get("version") for e in entries if isinstance(e, dict)}
    if UCP_VERSION in versions:
      active.add(name)
  changed = True
  while changed:
    changed = False
    for name in list(active):
      parent = BUSINESS_CAPABILITIES[name][2]
      if parent and parent not in active:
        active.discard(name)
        changed = True
  return active


def negotiate(header_value: str | None, settings: Settings) -> Negotiated:
  everything = set(BUSINESS_CAPABILITIES)
  url = parse_agent_header(header_value)
  if url is None:
    if settings.strict_negotiation:
      raise NegotiationError(
        400, "invalid_profile_url", "UCP-Agent header with profile is required."
      )
    return Negotiated(everything, None, "no UCP-Agent profile (lenient mode)")
  try:
    platform = fetch_profile(url, settings)
  except NegotiationError as exc:
    if settings.strict_negotiation:
      raise
    log.warning("Profile %s not usable (%s); continuing leniently", url, exc.code)
    return Negotiated(everything, url, f"profile not usable: {exc.code}")
  return Negotiated(intersect(platform), url, "negotiated")
