"""Agora SDP helpers."""

from __future__ import annotations

from typing import Any


class SDPParser:
    """SDP parser with modular dispatching."""

    @staticmethod
    def parse(sdp: str) -> dict[str, Any]:
        """Parse a SDP string into a dictionary."""
        parsed: dict[str, Any] = {"media": []}
        current_media: dict[str, Any] | None = None

        for line in (ln.strip() for ln in sdp.splitlines() if ln.strip()):
            if "=" not in line:
                continue

            line_type, value = line.split("=", 1)

            if line_type == "m":
                current_media = SDPParser._handle_media(parsed, value)
            elif line_type == "a":
                SDPParser._handle_attribute(current_media or parsed, parsed, value)
            else:
                SDPParser._handle_basic_line(parsed, line_type, value)

        return parsed

    @staticmethod
    def _handle_basic_line(target: dict, line_type: str, value: str) -> None:
        """Handles simple SDP lines like v, o, s."""
        match line_type:
            case "v":
                target["version"] = value
            case "s":
                target["name"] = value
            case "o":
                v = value.split()
                if len(v) >= 6:
                    target["origin"] = {
                        "username": v[0],
                        "sessionId": v[1],
                        "sessionVersion": v[2],
                        "netType": v[3],
                        "ipVer": v[4],
                        "address": v[5],
                    }

    @staticmethod
    def _handle_media(parsed: dict, value: str) -> dict[str, Any]:
        """Initializes a new media block."""
        v = value.split()
        new_media = {
            "type": v[0],
            "port": int(v[1]),
            "protocol": v[2],
            "payloads": " ".join(v[3:]),
            "rtp": [],
            "fmtp": [],
            "rtcpFb": [],
            "ext": [],
            "fingerprints": [],
            "attributes": {},
        }
        parsed["media"].append(new_media)
        return new_media

    @staticmethod
    def _handle_attribute(target: dict, global_parsed: dict, line_value: str) -> None:
        """Dispatches attribute parsing (a=...)."""
        parts = line_value.split(":", 1)
        attr = parts[0]
        val = parts[1] if len(parts) > 1 else None

        if attr in {"sendrecv", "sendonly", "recvonly", "inactive"}:
            target["direction"] = attr
            return

        match attr:
            case "ice-ufrag" | "ice-pwd" | "setup" | "mid" | "ice-options":
                key = "".join(word.capitalize() for word in attr.split("-"))
                target[key[0].lower() + key[1:]] = val
            case "fingerprint" if val:
                v = val.split()
                if len(v) >= 2:
                    fp = {"hash": v[0], "fingerprint": v[1]}
                    target.setdefault("fingerprints", []).append(fp)
                    target["fingerprint"] = fp
            case "rtpmap" if val:
                v = val.split(None, 1)
                m = v[1].split("/")
                target["rtp"].append(
                    {
                        "payload": int(v[0]),
                        "codec": m[0],
                        "rate": int(m[1]) if len(m) > 1 else 90000,
                        "encoding": m[2] if len(m) > 2 else None,
                    }
                )
            case "fmtp" if val:
                v = val.split(None, 1)
                target["fmtp"].append(
                    {"payload": int(v[0]), "config": v[1] if len(v) > 1 else ""}
                )
            case "extmap" if val:
                v = val.split()
                if len(v) >= 2:
                    ext_id = v[0].split("/", 1)[0]
                    target["ext"].append({"value": int(ext_id), "uri": v[1]})
            case "group" if val:
                v = val.split()
                global_parsed.setdefault("groups", []).append(
                    {"type": v[0], "mids": " ".join(v[1:])}
                )
            case "msid-semantic" if val:
                v = val.split()
                global_parsed["msidSemantic"] = {
                    "semantic": v[0],
                    "token": v[1] if len(v) > 1 else "",
                }


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
