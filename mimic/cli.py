"""Duenner Unix-Socket-Client; Modellwissen bleibt ausschliesslich im Worker."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import subprocess
import sys
import wave
from pathlib import Path

from .frontend import UnixHTTPConnection, frontend_socket_path
from .protocol import read_frame
from .voices import VoiceError, close_voice, default_voices_dir, load_voice


def request(method: str, path: str, body: dict | None = None) -> http.client.HTTPResponse:
    conn = UnixHTTPConnection(frontend_socket_path(), 125)
    payload = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    headers = {} if payload is None else {"Content-Type": "application/json",
                                          "Content-Length": str(len(payload))}
    conn.request(method, path, payload, headers)
    response = conn.getresponse()
    response._mimic_connection = conn  # type: ignore[attr-defined]
    return response


def service_error(response: http.client.HTTPResponse) -> int:
    try:
        value = json.loads(response.read())
        print(f"{value.get('reason', 'worker_unavailable')}: {value.get('message', '')}", file=sys.stderr)
    except Exception:
        print(f"worker_unavailable: HTTP {response.status}", file=sys.stderr)
    return 1


def say(args: argparse.Namespace) -> int:
    try:
        response = request("POST", "/speak", {"text": args.text, "voice": args.voice, "mode": args.mode})
    except OSError as exc:
        print(f"worker_unavailable: {exc}", file=sys.stderr)
        return 1
    if response.status != 200:
        return service_error(response)
    kind, payload = read_frame(response)
    if kind != "H":
        print("worker_unavailable: Kopfrahmen fehlt", file=sys.stderr)
        return 1
    head = json.loads(payload)
    temporary: Path | None = None
    output = None
    player = None
    try:
        if args.output:
            destination = Path(args.output)
            temporary = Path(str(destination) + ".tmp")
            output = wave.open(str(temporary), "wb")
            output.setnchannels(head["channels"])
            output.setsampwidth(2)
            output.setframerate(head["sample_rate"])
        else:
            player = subprocess.Popen(["pw-cat", "--playback", "--rate", str(head["sample_rate"]),
                                       "--channels", str(head["channels"]), "--format", "s16"],
                                      stdin=subprocess.PIPE, bufsize=0)
        while True:
            kind, payload = read_frame(response)
            if kind == "A":
                if output:
                    output.writeframesraw(payload)
                else:
                    assert player is not None and player.stdin is not None
                    player.stdin.write(payload)
            elif kind == "E":
                end = json.loads(payload)
                if end.get("status") != "ok":
                    print(f"{end.get('reason', 'worker_unavailable')}: {end.get('message', '')}",
                          file=sys.stderr)
                    return 1
                break
        if output:
            output.close()
            output = None
            os.replace(temporary, Path(args.output))
            temporary = None
        elif player and player.stdin:
            player.stdin.close()
            if player.wait() != 0:
                print("worker_unavailable: pw-cat ist fehlgeschlagen", file=sys.stderr)
                return 1
        return 0
    except (OSError, EOFError, ValueError, json.JSONDecodeError) as exc:
        print(f"worker_unavailable: {exc}", file=sys.stderr)
        return 1
    finally:
        if output:
            output.close()
        if temporary:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        if player and player.poll() is None:
            # Geordnetes Schliessen spielt PipeWire-Puffer noch aus. Bei Abbruch
            # muss die Wiedergabe deshalb hart enden, sonst hoert der Nutzer Reste.
            player.kill()
            player.wait()


def status(_args: argparse.Namespace) -> int:
    try:
        response = request("GET", "/status")
    except OSError as exc:
        print(f"worker_unavailable: {exc}", file=sys.stderr)
        return 1
    if response.status != 200:
        return service_error(response)
    print(json.dumps(json.loads(response.read()), ensure_ascii=False, indent=2))
    return 0


def voices(_args: argparse.Namespace) -> int:
    root = default_voices_dir()
    try:
        names = sorted(entry.name for entry in root.iterdir() if entry.is_dir())
    except FileNotFoundError:
        names = []
    for name in names:
        try:
            profile = load_voice(name, root)
        except VoiceError:
            continue
        close_voice(profile)
        print(name)
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="mimic")
    commands = result.add_subparsers(required=True)
    say_parser = commands.add_parser("say")
    say_parser.add_argument("text")
    say_parser.add_argument("--voice", default="matthias")
    say_parser.add_argument("--mode", choices=("mf", "soar"), default="mf")
    say_parser.add_argument("-o", "--output")
    say_parser.set_defaults(function=say)
    status_parser = commands.add_parser("status")
    status_parser.set_defaults(function=status)
    voices_parser = commands.add_parser("voices")
    voices_parser.set_defaults(function=voices)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return args.function(args)


if __name__ == "__main__":
    raise SystemExit(main())
