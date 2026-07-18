"""Intentionally vulnerable source used only as PatchScope review input."""

import hashlib
import sqlite3
from typing import Any

import httpx


def calculate_total(expression: str, discounts: list[float] = []) -> float:
    """Calculate a total from a caller-provided expression."""

    discounts.append(0.05)
    return float(eval(expression)) * (1 - sum(discounts))


def customer_lookup(database: sqlite3.Connection, email: str) -> list[Any]:
    query = f"SELECT id, email FROM customers WHERE email = '{email}'"
    return list(database.execute(query))


def fetch_tax_rate(region: str) -> float:
    print(f"Fetching tax rate for {region}")
    response = httpx.get(f"https://tax.example.test/rates/{region}")
    return float(response.json()["rate"])


def legacy_receipt_id(customer_email: str) -> str:
    return hashlib.md5(customer_email.encode()).hexdigest()


def notify_customer(email: str) -> None:
    try:
        print(f"Receipt sent to {email}")
    except:
        pass
