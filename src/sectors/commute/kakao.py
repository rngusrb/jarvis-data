"""카카오 로컬 — 좌표 근처의 지하철역과 동네 이름.

키는 머리말(`Authorization: KakaoAK …`)로 보낸다 — 주소에 안 들어가 요청 로그에 안 찍힌다. 집 좌표는
보내지 않는다(부르는 쪽 `naming.py` 가 집을 건너뛴다). 앱의 "카카오맵" 사용 설정이 꺼져 있으면 403
`disabled OPEN_MAP_AND_LOCAL` 이 온다(2026-11-09) — 무엇이 왔는지와 함께 예외로 올린다.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import httpx

BASE = "https://dapi.kakao.com/v2/local"
SUBWAY = "SW8"  # 카카오 장소 갈래 — 지하철역


async def _get(client: httpx.AsyncClient, key: str, path: str, params: Dict[str, Any]) -> Any:
    response = await client.get(
        f"{BASE}/{path}", params=params, headers={"Authorization": f"KakaoAK {key}"}
    )
    if response.status_code != 200:
        raise RuntimeError(f"카카오 {path} HTTP {response.status_code}: {response.text[:200]}")
    return response.json()


async def nearest_station(
    client: httpx.AsyncClient, key: str, lat: float, lon: float, radius: int = 200
) -> Optional[Tuple[str, int]]:
    """반경 안 가장 가까운 지하철역 — ("가람역 5호선", 152m). 없으면 None."""
    body = await _get(
        client,
        key,
        "search/category.json",
        {
            "category_group_code": SUBWAY,
            "x": lon,
            "y": lat,
            "radius": radius,
            "sort": "distance",
            "size": 1,
        },
    )
    docs = body.get("documents") or []
    if not docs:
        return None
    return str(docs[0]["place_name"]), int(float(docs[0].get("distance") or 0))


async def region(client: httpx.AsyncClient, key: str, lat: float, lon: float) -> Optional[str]:
    """좌표의 동네 — "가나구 다라동". 없으면 None."""
    body = await _get(client, key, "geo/coord2address.json", {"x": lon, "y": lat})
    docs = body.get("documents") or []
    if not docs:
        return None
    address = docs[0].get("address") or {}
    parts = [address.get("region_2depth_name"), address.get("region_3depth_name")]
    named = " ".join(str(p) for p in parts if p)
    return named or None


async def station_spot(
    client: httpx.AsyncClient, key: str, name: str, line: str
) -> Optional[Tuple[float, float]]:
    """역 이름으로 역의 자리(위도, 경도) — "동녘", "5호선" → 동녘역 5호선. 사용자의 좌표는 안
    보낸다."""
    body = await _get(
        client,
        key,
        "search/keyword.json",
        {"query": f"{name}역 {line}", "category_group_code": SUBWAY, "size": 5},
    )
    for doc in body.get("documents") or []:
        place = str(doc.get("place_name") or "")
        if place.startswith(f"{name}역") and line in place:
            return float(doc["y"]), float(doc["x"])
    return None
