"""Listen for new Pump.fun tokens and simulate trading with TP/SL.

This example combines ``listen-new-tokens/listen_logsubscribe.py`` and
``fetch_price.py``. It listens for new token creations via WebSocket, fetches
prices from the bonding curve account and simulates a buy with configurable
take-profit and stop-loss percentages.

Environment variables:
* ``SOLANA_NODE_WSS_ENDPOINT`` - WebSocket endpoint
* ``SOLANA_NODE_RPC_ENDPOINT`` - RPC HTTP endpoint
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
from typing import Final

import base58
import websockets
from construct import Flag, Int64ul
from construct import Struct as CStruct
from dotenv import load_dotenv
from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey

load_dotenv()

WSS_ENDPOINT = os.getenv("SOLANA_NODE_WSS_ENDPOINT")
RPC_ENDPOINT = os.getenv("SOLANA_NODE_RPC_ENDPOINT")
PUMP_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")

LAMPORTS_PER_SOL: Final[int] = 1_000_000_000
TOKEN_DECIMALS: Final[int] = 6
EXPECTED_DISCRIMINATOR: Final[bytes] = struct.pack("<Q", 6966180631402821399)
CREATE_DISCRIMINATOR_LEN: Final[int] = 8

TAKE_PROFIT_PCT: Final[float] = 1.0  # 100% profit target
STOP_LOSS_PCT: Final[float] = 0.5  # 50% stop loss

ERR_INVALID_RESERVE = "Invalid reserve state"
ERR_NO_CURVE_DATA = "Invalid curve state: No data"
ERR_INVALID_DISCRIMINATOR = "Invalid curve state discriminator"

SIM_TASKS: list[asyncio.Task] = []


def parse_create_instruction(data: bytes) -> dict[str, str] | None:
    """Decode the Create instruction data."""
    if len(data) < CREATE_DISCRIMINATOR_LEN:
        return None
    offset = CREATE_DISCRIMINATOR_LEN
    parsed_data: dict[str, str] = {}

    fields = [
        ("name", "string"),
        ("symbol", "string"),
        ("uri", "string"),
        ("mint", "publicKey"),
        ("bondingCurve", "publicKey"),
        ("user", "publicKey"),
        ("creator", "publicKey"),
    ]

    try:
        for field_name, field_type in fields:
            if field_type == "string":
                length = struct.unpack("<I", data[offset : offset + 4])[0]
                offset += 4
                value = data[offset : offset + length].decode("utf-8")
                offset += length
            else:
                value = base58.b58encode(data[offset : offset + 32]).decode("utf-8")
                offset += 32
            parsed_data[field_name] = value
    except Exception:  # noqa: BLE001
        return None

    return parsed_data


class BondingCurveState:
    _STRUCT = CStruct(
        "virtual_token_reserves" / Int64ul,
        "virtual_sol_reserves" / Int64ul,
        "real_token_reserves" / Int64ul,
        "real_sol_reserves" / Int64ul,
        "token_total_supply" / Int64ul,
        "complete" / Flag,
    )

    def __init__(self, data: bytes) -> None:
        parsed = self._STRUCT.parse(data[8:])
        self.__dict__.update(parsed)


def calculate_bonding_curve_price(curve_state: BondingCurveState) -> float:
    if (
        curve_state.virtual_token_reserves <= 0
        or curve_state.virtual_sol_reserves <= 0
    ):
        raise ValueError(ERR_INVALID_RESERVE)
    return (curve_state.virtual_sol_reserves / LAMPORTS_PER_SOL) / (
        curve_state.virtual_token_reserves / 10**TOKEN_DECIMALS
    )


async def get_price(conn: AsyncClient, curve_address: Pubkey) -> float:
    response = await conn.get_account_info(curve_address, encoding="base64")
    if not response.value or not response.value.data:
        raise ValueError(ERR_NO_CURVE_DATA)
    data = response.value.data
    if data[:8] != EXPECTED_DISCRIMINATOR:
        raise ValueError(ERR_INVALID_DISCRIMINATOR)
    state = BondingCurveState(data)
    return calculate_bonding_curve_price(state)


async def simulate_trade(curve_address: str, token_name: str) -> None:
    async with AsyncClient(RPC_ENDPOINT) as conn:
        curve_pubkey = Pubkey.from_string(curve_address)
        entry_price = await get_price(conn, curve_pubkey)
        tp_price = entry_price * (1 + TAKE_PROFIT_PCT)
        sl_price = entry_price * (1 - STOP_LOSS_PCT)
        print(
            f"Simulated buy of {token_name} at {entry_price:.10f} SOL:\n"
            f"  TP {tp_price:.10f} SOL | SL {sl_price:.10f} SOL"
        )
        while True:
            await asyncio.sleep(1)
            price = await get_price(conn, curve_pubkey)
            if price >= tp_price:
                print(f"{token_name}: take profit hit at {price:.10f} SOL")
                return
            if price <= sl_price:
                print(f"{token_name}: stop loss hit at {price:.10f} SOL")
                return


async def listen_for_new_tokens() -> None:
    while True:
        try:
            async with websockets.connect(WSS_ENDPOINT) as websocket:
                subscription_message = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "logsSubscribe",
                        "params": [
                            {"mentions": [str(PUMP_PROGRAM_ID)]},
                            {"commitment": "processed"},
                        ],
                    }
                )
                await websocket.send(subscription_message)
                await websocket.recv()  # Wait for subscription confirmation
                while True:
                    response = await websocket.recv()
                    data = json.loads(response)
                    if data.get("method") != "logsNotification":
                        continue
                    log_data = data["params"]["result"]["value"]
                    logs = log_data.get("logs", [])
                    if not any(
                        "Program log: Instruction: Create" in entry for entry in logs
                    ):
                        continue
                    for log in logs:
                        if "Program data:" not in log:
                            continue
                        encoded_data = log.split(": ")[1]
                        decoded_data = base64.b64decode(encoded_data)
                        parsed = parse_create_instruction(decoded_data)
                        if not parsed or "bondingCurve" not in parsed:
                            continue
                        name = parsed.get("name", "unknown")
                        curve = parsed["bondingCurve"]
                        print(f"New token detected: {name} ({curve})")
                        task = asyncio.create_task(simulate_trade(curve, name))
                        SIM_TASKS.append(task)
        except Exception as exc:  # noqa: BLE001
            print(f"Connection error: {exc}. Reconnecting...")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(listen_for_new_tokens())
