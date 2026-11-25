"""Strict IOL v2 readers. Schema: retained official swagger, 2026-09-23.

The schema is not an empirical certification of account/fill semantics. Unknown
shapes fail closed; commissioning must compare these readings with the account.
No token, environment file or connection is created by importing this module.
"""
from decimal import Decimal
from zoneinfo import ZoneInfo

from argbot.execution import SETTLEMENTS, AccountView, OrderView, integer, money, parse_timestamp


OPEN = frozenset({'iniciada', 'en_Proceso', 'parcialmente_Terminada',
                  'pendiente_Cancelacion', 'parcialmente_Terminada_Con_Pedido_Cancelacion'})
CANCELLED = frozenset({'cancelada', 'cancelada_Por_Vencimiento_Validez'})


def operation_id(value):
    return str(integer(value, positive=True))


def account_identity(payload):
    accounts = payload['cuentas']
    if not isinstance(accounts, list):
        raise ValueError('Incomplete account list')
    candidates = [a for a in accounts if a['tipo'] == 'inversion_Argentina_Pesos']
    if len(candidates) != 1:
        raise ValueError('Exactly one Argentine ARS investment account required')
    account = candidates[0]
    if account['moneda'] != 'peso_Argentino' or account['estado'] != 'operable':
        raise ValueError('Account currency/state not operable')
    if not isinstance(account['numero'], str) or not account['numero'].strip():
        raise ValueError('Missing broker account identity')
    # Include broker and account type to avoid collisions with other accounts.
    return 'IOL:inversion_Argentina_Pesos:' + account['numero'].strip(), account


def normalize_account(payload, portfolio, operations):
    identity, account = account_identity(payload)
    # The executor trades a24horas. Top-level disponible is immediate cash,
    # not the cumulative purchasing power for T1 (observed 2026-09-30).
    balances = account['saldos']
    if not isinstance(balances, list):
        raise ValueError('Missing settlement balances')
    t1 = [row for row in balances if row['liquidacion'] == 'hrs24']
    if len(t1) != 1:
        raise ValueError('Exactly one T1 balance required')
    available = money(t1[0]['disponibleOperar'])
    if portfolio['pais'] != 'argentina' or not isinstance(portfolio['activos'], list):
        raise ValueError('Incomplete Argentine portfolio')
    positions = {}
    for row in portfolio['activos']:
        symbol = row['titulo']['simbolo']
        if not isinstance(symbol, str) or not symbol:
            raise ValueError('Missing holding identity')
        qty = integer(row['cantidad'])
        positions[symbol] = positions.get(symbol, 0) + qty
    if not isinstance(operations, list):
        raise ValueError('Incomplete active order list')
    ids = []
    for row in operations:
        # A new broker status must be reviewed, not silently omitted.
        if row['estado'] in OPEN:
            ids.append(operation_id(row['numero']))
        elif row['estado'] not in CANCELLED | {'terminada'}:
            raise ValueError('Unknown order state')
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate active order identifier')
    return AccountView(str(available), positions, frozenset(ids), identity,
                       'hrs24.disponibleOperar')


def order_instant(value, naive_timezone):
    parsed = parse_timestamp(value)
    if parsed.tzinfo is not None:
        return parsed
    if naive_timezone != 'America/Argentina/Buenos_Aires':
        raise ValueError('Naive broker order time requires explicit ART convention')
    return parsed.replace(tzinfo=ZoneInfo(naive_timezone))


def normalize_order(payload, account_id, *, naive_timezone=None):
    # The API has been seen returning both spellings of the market code.
    # Keep an explicit alias list, not unrestricted case/format coercion.
    # Each check names its own field, so a refusal says which one differed.
    if payload['mercado'] not in ('bCBA', 'bcba'):
        raise ValueError('Order market mismatch')
    if payload['moneda'] != 'peso_Argentino':
        raise ValueError('Order currency mismatch')
    # An order sent at one settlement can be recorded by the broker at another.
    # Any settlement IOL documents for an order detail is accepted and kept on the
    # order; it moves when the cash arrives, which the cash check accounts for.
    if payload['plazo'] not in SETTLEMENTS:
        raise ValueError('Order settlement not recognised')
    if payload['modalidad'] != 'precio_Limite':
        raise ValueError('Order type mismatch')
    status = payload['estadoActual']
    if status in OPEN:
        status = 'open'
    elif status in CANCELLED:
        status = 'cancelled'
    elif status == 'terminada':
        status = 'filled'
    else:
        raise ValueError('Unsupported order state')
    side = {'compra': 'BUY', 'venta': 'SELL'}[payload['tipo']]
    submitted = order_instant(payload['fechaAlta'], naive_timezone)
    naive_times = parse_timestamp(payload['fechaAlta']).tzinfo is None
    fills = payload['operaciones']
    if not isinstance(fills, list):
        raise ValueError('Missing confirmed executions')
    qty, gross = 0, Decimal(0)
    for fill in fills:
        naive_times |= parse_timestamp(fill['fecha']).tzinfo is None
        # fechaAlta keeps milliseconds while execution times are cut to the second, so
        # an execution can read a fraction earlier than its order. Compare at the execution's
        # own precision; a whole second earlier is still refused.
        floor = submitted if '.' in fill['fecha'] else submitted.replace(microsecond=0)
        if order_instant(fill['fecha'], naive_timezone) < floor:
            raise ValueError('Execution precedes order')
        count, price = integer(fill['cantidad'], positive=True), money(fill['precio'])
        if price <= 0:
            raise ValueError('Invalid execution price')
        qty += count
        gross += count * price
    requested = integer(payload['cantidad'], positive=True)
    if status == 'filled' and 0 < qty < requested:
        # The broker can report a partly executed order whose rest was cancelled as
        # "terminada" too. The executions are final: the rest is cancelled.
        status = 'cancelled'
    if qty > requested or (status == 'filled' and qty != requested):
        raise ValueError('Execution details do not reconcile with order')
    if 'cantidadOperada' in payload and integer(payload['cantidadOperada']) != qty:
        raise ValueError('Conflicting cumulative execution quantity')
    fees = money(payload['arancelesARS'])  # explicit aggregate; absent is not zero
    if money(payload['arancelesUSD']) != 0 or (qty == 0 and fees != 0):
        raise ValueError('Unsupported charges')
    if 'montoOperacion' in payload and money(payload['montoOperacion']) != gross:
        raise ValueError('Executed gross disagrees with confirmed executions')
    if 'aranceles' in payload:
        charges = payload['aranceles']
        if not isinstance(charges, list):
            raise ValueError('Invalid charge breakdown')
        subtotal = Decimal(0)
        for charge in charges:
            if charge['moneda'] not in ('PESO_ARGENTINO', 'peso_Argentino'):
                raise ValueError('Unsupported charge currency')
            subtotal += money(charge['neto']) + money(charge['iva'])
        if subtotal != fees:
            raise ValueError('Charge aggregate disagrees with breakdown')
    return OrderView(operation_id(payload['numero']), payload['simbolo'], side, requested,
                     str(money(payload['precio'])), submitted.isoformat(), status,
                     qty, str(gross), str(fees), account_id,
                     'explicit_ART_convention' if naive_times else 'source_offset', payload['fechaAlta'],
                     payload['plazo'])


class IOLReaders:
    """Account ownership comes from /estadocuenta on the SAME client/session.

    Order detail has no account-number field. Do not invent one: establish the
    account before each detail request; the account endpoint is rechecked on
    every reconciliation. This requires exclusive use of the supplied client.
    """
    def __init__(self, *, naive_order_timezone=None):
        if naive_order_timezone not in (None, 'America/Argentina/Buenos_Aires'):
            raise ValueError('Unsupported broker order timezone convention')
        self.naive_order_timezone = naive_order_timezone

    def account(self, client):
        account = client.get_estado_cuenta()
        portfolio = client.get_portafolio('argentina')
        orders = client.get_operaciones(estado='Pendientes')
        return normalize_account(account, portfolio, orders)

    def order(self, client, broker_id):
        identity, _ = account_identity(client.get_estado_cuenta())
        detail = normalize_order(client.get_operacion(int(broker_id)), identity,
                                 naive_timezone=self.naive_order_timezone)
        if detail.broker_id != broker_id:
            raise ValueError('Broker returned another order')
        return detail
