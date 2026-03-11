"""Agora SDP helpers."""

from __future__ import annotations

from typing import Any


class SDPParser:
    """Small SDP parser used to build ORTC capabilities for join_v3."""

    @staticmethod
    def parse(sdp: str) -> dict[str, Any]:
        """Parse a SDP string into a dictionary."""
        parsed: dict[str, Any] = {"media": []}
        current_media: dict[str, Any] | None = None

        for raw_line in sdp.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            parts = line.split("=", 1)
            if len(parts) != 2:
                continue
            line_type, line_value = parts

            if line_type == "v":
                parsed["version"] = line_value
            elif line_type == "o":
                values = line_value.split()
                if len(values) >= 6:
                    parsed["origin"] = {
                        "username": values[0],
                        "sessionId": values[1],
                        "sessionVersion": values[2],
                        "netType": values[3],
                        "ipVer": values[4],
                        "address": values[5],
                    }
            elif line_type == "s":
                parsed["name"] = line_value
            elif line_type == "m":
                values = line_value.split()
                current_media = {
                    "type": values[0],
                    "port": int(values[1]),
                    "protocol": values[2],
                    "payloads": " ".join(values[3:]),
                    "rtp": [],
                    "fmtp": [],
                    "rtcpFb": [],
                    "ext": [],
                    "fingerprints": [],
                    "attributes": {},
                }
                parsed["media"].append(current_media)
            elif line_type == "a":
                attribute_parts = line_value.split(":", 1)
                attribute = attribute_parts[0]
                value = attribute_parts[1] if len(attribute_parts) > 1 else None

                target = current_media if current_media is not None else parsed

                if attribute == "ice-ufrag":
                    target["iceUfrag"] = value
                elif attribute == "ice-pwd":
                    target["icePwd"] = value
                elif attribute == "fingerprint" and value:
                    values = value.split()
                    if len(values) >= 2:
                        fingerprint = {
                            "hash": values[0],
                            "fingerprint": values[1],
                        }
                        target.setdefault("fingerprints", []).append(fingerprint)
                        target["fingerprint"] = fingerprint
                elif attribute == "setup":
                    target["setup"] = value
                elif attribute == "mid":
                    target["mid"] = value
                elif attribute in {"sendrecv", "sendonly", "recvonly", "inactive"}:
                    target["direction"] = attribute
                elif attribute == "ice-options":
                    target["iceOptions"] = value
                elif attribute == "rtpmap" and value:
                    values = value.split(None, 1)
                    payload = int(values[0])
                    rtp_map = values[1].split("/")
                    target["rtp"].append(
                        {
                            "payload": payload,
                            "codec": rtp_map[0],
                            "rate": int(rtp_map[1]) if len(rtp_map) > 1 else 90000,
                            "encoding": rtp_map[2] if len(rtp_map) > 2 else None,
                        }
                    )
                elif attribute == "fmtp" and value:
                    values = value.split(None, 1)
                    target["fmtp"].append(
                        {
                            "payload": int(values[0]),
                            "config": values[1] if len(values) > 1 else "",
                        }
                    )
                elif attribute == "rtcp-fb" and value:
                    values = value.split()
                    target["rtcpFb"].append(
                        {
                            "payload": int(values[0]),
                            "type": values[1] if len(values) > 1 else "",
                            "subtype": (
                                " ".join(values[2:]) if len(values) > 2 else None
                            ),
                        }
                    )
                elif attribute == "extmap" and value:
                    values = value.split()
                    if len(values) >= 2:
                        target["ext"].append(
                            {
                                "value": int(values[0]),
                                "uri": values[1],
                            }
                        )
                elif attribute == "group" and value:
                    parsed.setdefault("groups", []).append(
                        {
                            "type": value.split()[0],
                            "mids": " ".join(value.split()[1:]),
                        }
                    )
                elif attribute == "msid-semantic" and value:
                    values = value.split()
                    parsed["msidSemantic"] = {
                        "semantic": values[0],
                        "token": values[1] if len(values) > 1 else "",
                    }

        return parsed


def parse_offer_to_ortc(offer_sdp: str) -> dict[str, Any]:
    """Parse SDP offer to ORTC structure expected by join_v3."""
    parsed = SDPParser.parse(offer_sdp)

    ice_parameters: dict[str, Any] = {}
    dtls_parameters: dict[str, Any] = {}

    for media in parsed.get("media", []):
        if not ice_parameters and "iceUfrag" in media:
            ice_parameters = {
                "iceUfrag": media.get("iceUfrag"),
                "icePwd": media.get("icePwd"),
            }
        if not dtls_parameters and media.get("fingerprints"):
            dtls_parameters = {
                "fingerprints": [
                    {
                        "hashFunction": fingerprint.get("hash"),
                        "fingerprint": fingerprint.get("fingerprint"),
                    }
                    for fingerprint in media.get("fingerprints", [])
                ]
            }

    if not ice_parameters and "iceUfrag" in parsed:
        ice_parameters = {
            "iceUfrag": parsed.get("iceUfrag"),
            "icePwd": parsed.get("icePwd"),
        }

    if not dtls_parameters and parsed.get("fingerprints"):
        dtls_parameters = {
            "fingerprints": [
                {
                    "hashFunction": fingerprint.get("hash"),
                    "fingerprint": fingerprint.get("fingerprint"),
                }
                for fingerprint in parsed.get("fingerprints", [])
            ]
        }

    dtls_parameters["role"] = "client"

    send_caps: dict[str, list[dict[str, Any]]] = {
        "audioCodecs": [],
        "audioExtensions": [],
        "videoCodecs": [],
        "videoExtensions": [],
    }
    recv_caps: dict[str, list[dict[str, Any]]] = {
        "audioCodecs": [],
        "audioExtensions": [],
        "videoCodecs": [],
        "videoExtensions": [],
    }

    for media in parsed.get("media", []):
        media_type = media.get("type")
        direction = media.get("direction", "sendrecv")

        codecs = []
        for rtp in media.get("rtp", []):
            payload_type = rtp.get("payload")
            codec = {
                "payloadType": payload_type,
                "rtpMap": {
                    "encodingName": rtp.get("codec"),
                    "clockRate": rtp.get("rate"),
                    "encodingParameters": rtp.get("encoding"),
                },
                "rtcpFeedbacks": [],
                "fmtp": {"parameters": {}},
            }

            for feedback in media.get("rtcpFb", []):
                if feedback.get("payload") == payload_type:
                    codec["rtcpFeedbacks"].append(
                        {
                            "type": feedback.get("type"),
                            "parameter": feedback.get("subtype"),
                        }
                    )

            for fmtp in media.get("fmtp", []):
                if fmtp.get("payload") != payload_type:
                    continue
                for part in str(fmtp.get("config", "")).split(";"):
                    if "=" not in part:
                        continue
                    key, value = part.split("=", 1)
                    codec["fmtp"]["parameters"][key.strip()] = value.strip()

            codecs.append(codec)

        extensions = [
            {
                "entry": extension.get("value"),
                "extensionName": extension.get("uri"),
            }
            for extension in media.get("ext", [])
        ]

        if direction == "sendonly":
            targets = [send_caps]
        elif direction == "recvonly":
            targets = [recv_caps]
        else:
            targets = [send_caps, recv_caps]

        for target in targets:
            if media_type == "video":
                target["videoCodecs"].extend(codecs)
                target["videoExtensions"].extend(extensions)
            elif media_type == "audio":
                target["audioCodecs"].extend(codecs)
                target["audioExtensions"].extend(extensions)

    return {
        "iceParameters": ice_parameters,
        "dtlsParameters": dtls_parameters,
        "rtpCapabilities": {
            "send": send_caps,
            "recv": recv_caps,
        },
        "version": "2",
    }
