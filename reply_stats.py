#!/usr/bin/env python3
"""Telegram reply statistics. Python 3.10+, Telethon 1.x for collection only."""
import argparse
import asyncio
import csv
import getpass
import html
import json
import math
import os
import random
import statistics
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

UTC = timezone.utc
ROLES = ('me', 'friend')
LABELS = {'me': 'Вы', 'friend': 'Собеседник'}
DEFAULT = {
    'report_timezone': 'Asia/Barnaul',
    'me': {'timezone': 'Asia/Barnaul', 'sleep_start': '00:00', 'sleep_end': '08:00'},
    'friend': {'timezone': 'Asia/Barnaul', 'sleep_start': '00:00', 'sleep_end': '08:00'},
    'long_gap_hours': 24,
    'min_period_samples': 5,
}


def parse_dt(s):
    dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('Время в данных должно содержать часовой пояс')
    return dt.astimezone(UTC)


def load_config(path):
    c = json.loads(json.dumps(DEFAULT))
    if path.exists():
        x = json.loads(path.read_text(encoding='utf-8'))
        for k, v in x.items():
            if k in ROLES:
                c[k].update(v)
            else:
                c[k] = v
    ZoneInfo(c['report_timezone'])
    for role in ROLES:
        ZoneInfo(c[role]['timezone'])
        for key in ('sleep_start', 'sleep_end'):
            time.fromisoformat(c[role][key])
    if not math.isfinite(c['long_gap_hours']) or c['long_gap_hours'] <= 0:
        raise ValueError('long_gap_hours должен быть положительным')
    if c['min_period_samples'] < 1:
        raise ValueError('min_period_samples должен быть >= 1')
    return c


def awake_seconds(start, end, settings):
    """Subtract recipient's recurring sleep, intersecting intervals in UTC (DST-safe)."""
    if end <= start:
        return 0.0
    zone = ZoneInfo(settings['timezone'])
    a = time.fromisoformat(settings['sleep_start'])
    b = time.fromisoformat(settings['sleep_end'])
    total = (end - start).total_seconds()
    if a == b:  # Explicitly disable the sleep filter.
        return total
    day = start.astimezone(zone).date() - timedelta(days=1)
    last = end.astimezone(zone).date()
    while day <= last:
        left = datetime.combine(day, a, zone).astimezone(UTC)
        right_day = day + timedelta(days=1) if b <= a else day
        right = datetime.combine(right_day, b, zone).astimezone(UTC)
        total -= max(0.0, (min(end, right) - max(start, left)).total_seconds())
        day += timedelta(days=1)
    return max(total, 0.0)


def percentile(values, p):
    if not values:
        return None
    v = sorted(values)
    pos = (len(v) - 1) * p
    a = math.floor(pos)
    b = math.ceil(pos)
    return v[a] + (v[b] - v[a]) * (pos - a)


def metrics(events):
    v = [e['awake_first_s'] for e in events]
    raw = [e['raw_first_s'] for e in events]
    last = [e['awake_last_s'] for e in events]
    return {
        'n': len(v),
        'mean_min': statistics.mean(v) / 60 if v else None,
        'median_min': statistics.median(v) / 60 if v else None,
        'p90_min': percentile(v, .9) / 60 if v else None,
        'raw_mean_min': statistics.mean(raw) / 60 if raw else None,
        'last_mean_min': statistics.mean(last) / 60 if last else None,
        'last_median_min': statistics.median(last) / 60 if last else None,
        'within_5m_pct': sum(x <= 300 for x in v) * 100 / len(v) if v else None,
        'within_1h_pct': sum(x <= 3600 for x in v) * 100 / len(v) if v else None,
        'zero_awake_n': sum(x == 0 for x in v),
    }


def period_keys(dt):
    return {'daily': dt.date().isoformat(), 'monthly': dt.strftime('%Y-%m'),
            'weekly': (dt.date() - timedelta(days=dt.weekday())).isoformat(),
            'weekday': str(dt.weekday()), 'hour': f'{dt.hour:02}'}


def analyze(messages, cfg, observed_until):
    messages = sorted(messages, key=lambda m: (m['ts'], m.get('seq', 0)))
    zone = ZoneInfo(cfg['report_timezone'])
    blocks, events = [], []
    initiations = Counter()
    limit = cfg['long_gap_hours'] * 3600
    previous = None
    for m in messages:
        dt = parse_dt(m['ts'])
        role = m['who']
        if role not in ROLES:
            raise ValueError('Неизвестный отправитель')
        gap = (dt - previous).total_seconds() if previous else None
        new_session = gap is None or gap > limit
        if new_session:
            initiations[role] += 1
        if blocks and blocks[-1]['who'] == role and not new_session:
            blocks[-1]['last'] = dt
            blocks[-1]['count'] += 1
        else:
            if blocks and blocks[-1]['who'] != role:
                old = blocks[-1]
                raw_first = (dt - old['first']).total_seconds()
                raw_last = (dt - old['last']).total_seconds()
                events.append({
                    'responder': role, 'start': old['first'].isoformat(),
                    'last_prompt': old['last'].isoformat(), 'reply': dt.isoformat(),
                    'prompt_count': old['count'], 'raw_first_s': raw_first,
                    'raw_last_s': raw_last,
                    'awake_first_s': awake_seconds(old['first'], dt, cfg[role]),
                    'awake_last_s': awake_seconds(old['last'], dt, cfg[role]),
                    # A fresh conversation may be mistaken for a reply. Retain separately.
                    'long_gap': raw_last > limit,
                })
            blocks.append({'who': role, 'first': dt, 'last': dt, 'count': 1})
        previous = dt
    accepted = [e for e in events if not e['long_gap']]
    grouped = {kind: {} for kind in ('daily', 'weekly', 'monthly', 'weekday', 'hour')}
    # Calendar bins include empty days/months: no reply is represented by null, never zero.
    if messages:
        day = parse_dt(messages[0]['ts']).astimezone(zone).date()
        end_day = parse_dt(messages[-1]['ts']).astimezone(zone).date()
        while day <= end_day:
            for kind, key in period_keys(datetime.combine(day, time(), zone)).items():
                if kind not in ('weekday', 'hour'):
                    grouped[kind].setdefault(key, {'me': [], 'friend': [], 'messages': Counter()})
            day += timedelta(days=1)
    for kind, keys in (('weekday', [str(x) for x in range(7)]), ('hour', [f'{x:02}' for x in range(24)])):
        for key in keys:
            grouped[kind][key] = {'me': [], 'friend': [], 'messages': Counter()}
    for m in messages:
        for kind, key in period_keys(parse_dt(m['ts']).astimezone(zone)).items():
            grouped[kind].setdefault(key, {'me': [], 'friend': [], 'messages': Counter()})['messages'][m['who']] += 1
    # Attribute delay to the date the waiting began, not the date of the reply.
    for e in accepted:
        for kind, key in period_keys(parse_dt(e['start']).astimezone(zone)).items():
            grouped[kind][key][e['responder']].append(e)
    aggregates = {}
    for kind, buckets in grouped.items():
        aggregates[kind] = []
        for key, bucket in sorted(buckets.items(), key=lambda pair: int(pair[0]) if kind == 'weekday' else pair[0]):
            row = {'period': key}
            for role in ROLES:
                row[role] = metrics(bucket[role])
                row[role]['messages'] = bucket['messages'][role]
            aggregates[kind].append(row)
    summary = {}
    for role in ROLES:
        own = [e for e in accepted if e['responder'] == role]
        all_own = [e for e in events if e['responder'] == role]
        summary[role] = metrics(own)
        summary[role].update({
            'messages': sum(m['who'] == role for m in messages),
            'types': dict(Counter(m.get('kind', 'other') for m in messages if m['who'] == role)),
            'session_starts': initiations[role],
            'long_gap_count': sum(e['long_gap'] for e in all_own),
            'including_long_gaps': metrics(all_own),
        })
    pending = None
    if blocks:
        b = blocks[-1]
        recipient = 'friend' if b['who'] == 'me' else 'me'
        pending = {'waiting_for': recipient, 'start': b['first'].isoformat(),
                   'raw_s': max(0, (observed_until - b['first']).total_seconds()),
                   'awake_s': awake_seconds(b['first'], observed_until, cfg[recipient])}
    changes = []
    monthly = aggregates['monthly']
    for before, after in zip(monthly, monthly[1:]):
        for role in ROLES:
            a, b = before[role], after[role]
            if min(a['n'], b['n']) >= cfg['min_period_samples']:
                changes.append({'who': role, 'from': before['period'], 'to': after['period'],
                                'previous_n': a['n'], 'n': b['n'],
                                'median_delta_min': b['median_min'] - a['median_min'],
                                'median_ratio': b['median_min'] / a['median_min'] if a['median_min'] else None})
    return {'summary': summary, 'periods': aggregates, 'events': events,
            'pending': pending, 'monthly_changes': changes}


def fmt(value):
    return '—' if value is None else f'{value:,.1f}'.replace(',', ' ')


def table(headers, rows):
    esc = lambda v: html.escape(str(v))
    return '<div class="scroll"><table><thead><tr>' + ''.join(f'<th>{esc(h)}</th>' for h in headers) + '</tr></thead><tbody>' + ''.join('<tr>' + ''.join(f'<td>{esc(v)}</td>' for v in row) + '</tr>' for row in rows) + '</tbody></table></div>'


def chart(rows, metric, title, ylabel, minimum=1):
    """Self-contained SVG, no network/CDN dependencies, nulls create gaps."""
    if not rows:
        return ''
    width = max(820, len(rows) * 9)
    height, left, right, top, bottom = 300, 64, 24, 24, 58
    values = [r[role].get(metric) for r in rows for role in ROLES
              if r[role]['n'] >= minimum or metric == 'messages']
    values = [v for v in values if v is not None]
    maximum = max(values + [1]) * 1.08
    x = lambda i: left + i * (width - left - right) / max(1, len(rows) - 1)
    y = lambda v: height - bottom - v * (height - top - bottom) / maximum
    parts = [f'<h3>{html.escape(title)}</h3><p class="muted">{html.escape(ylabel)} · синий: вы · оранжевый: собеседник</p>',
             f'<div class="scroll"><svg role="img" aria-label="{html.escape(title)}" viewBox="0 0 {width} {height}" style="min-width:{min(width, 1100)}px">']
    for i in range(5):
        v = maximum * i / 4
        parts.append(f'<path d="M {left} {y(v)} H {width-right}" stroke="#e2e8f0"/><text x="{left-8}" y="{y(v)+4}" text-anchor="end">{v:.1f}</text>')
    for role, color in (('me', '#2563eb'), ('friend', '#ea580c')):
        segment = []
        for i, row in enumerate(rows):
            v = row[role].get(metric)
            valid = v is not None and (metric == 'messages' or row[role]['n'] >= minimum)
            if not valid:
                if segment:
                    parts.append(f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{" ".join(segment)}"/>')
                segment = []
                continue
            segment.append(f'{x(i)},{y(v)}')
            tip = html.escape(f"{row['period']} · {LABELS[role]}: {fmt(v)}; ответов {row[role]['n']}")
            parts.append(f'<circle cx="{x(i)}" cy="{y(v)}" r="3" fill="{color}"><title>{tip}</title></circle>')
        if segment:
            parts.append(f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{" ".join(segment)}"/>')
    step = max(1, math.ceil(len(rows) / 10))
    for i, r in enumerate(rows):
        if i % step == 0 or i == len(rows)-1:
            parts.append(f'<text x="{x(i)}" y="{height-28}" text-anchor="middle">{html.escape(r["period"])}</text>')
    parts.append('</svg></div>')
    return ''.join(parts)


def render_report(payload):
    cfg, data = payload['config'], payload['analysis']
    summary = data['summary']
    body = ['<h1>Как быстро мы отвечаем друг другу</h1>',
            '<p>ДЕМОНСТРАЦИЯ: синтетические данные.</p>' if payload['demo'] else '',
            f'<p class="muted">Период: {html.escape(payload["start"])} — {html.escape(payload["end"])} · Часовой пояс отчёта: {html.escape(cfg["report_timezone"])}</p>',
            '<p>Это задержка до следующего сообщения другого участника, а не время после прочтения и не оценка отношения к человеку. Ответ может относиться к другой теме. Ночь исключается по заданному расписанию получателя.</p>']
    rows = []
    fields = [('Сообщений', 'messages'), ('Учтённых ответов', 'n'), ('Среднее, мин', 'mean_min'),
              ('Медиана, мин', 'median_min'), ('90% ответов не дольше, мин', 'p90_min'),
              ('Среднее без исключения ночи, мин', 'raw_mean_min'),
              ('Среднее от последнего сообщения, мин', 'last_mean_min'),
              ('Ответов до 5 минут, %', 'within_5m_pct'), ('Ответов до часа, %', 'within_1h_pct'),
              ('Ответов с нулевым бодрствующим ожиданием', 'zero_awake_n'),
              ('Начатых сессий', 'session_starts'), ('Переходов после долгой паузы', 'long_gap_count')]
    for label, field in fields:
        rows.append([label] + [fmt(summary[r][field]) for r in ROLES])
    body.append(table(['Показатель', 'Вы', 'Собеседник'], rows))
    body.append('<h2>Правила расчёта</h2><ul><li>Серия: сообщения одного участника до смены автора или долгого перерыва. Главная задержка — от первого сообщения серии; от последнего также сохранена.</li>')
    body.append(f'<li>Переходы после паузы более {cfg["long_gap_hours"]} ч от последнего сообщения вынесены отдельно. Это условная граница новой беседы, а не установленный факт отсутствия ответа.</li>')
    for role in ROLES:
        s = cfg[role]
        body.append(f'<li>{LABELS[role]}: сон {s["sleep_start"]}–{s["sleep_end"]}, {html.escape(s["timezone"])}. Одинаковые начало и конец отключают фильтр.</li>')
    body.append('<li>Период ответа определяется датой начала ожидания. Неизвестная задержка показана прочерком; сообщений может быть много при малом числе смен автора.</li><li>Первое сообщение периода не имеет известного предшественника; последнее ожидание не входит в среднее. Конец выгрузки не означает, что ответ никогда не придёт. Удалённые сообщения и история за границами периода неизвестны.</li><li>Дневные точки показаны при любом числе ответов. Маленькие выборки шумные; сравнения месяцев требуют минимального числа ответов.</li></ul>')
    pending = data['pending']
    if pending:
        body.append(f'<p>Последняя серия: ожидание сообщения от «{LABELS[pending["waiting_for"]]}», {fmt(pending["awake_s"]/60)} мин без сна на момент окончания наблюдения. Это не обязательно вопрос, требующий ответа.</p>')
    body.append('<h2>Когда ответы замедлились</h2>')
    changes = sorted(data['monthly_changes'], key=lambda r: r['median_delta_min'], reverse=True)
    body.append(table(['Кто', 'Сравнение месяцев', 'Ответов раньше / сейчас', 'Изменение медианы, мин', 'Во сколько раз'],
                      [[LABELS[c['who']], c['from']+' → '+c['to'], f'{c["previous_n"]} / {c["n"]}', fmt(c['median_delta_min']), fmt(c['median_ratio'])] for c in changes]))
    body.append(f'<p class="muted">Положительное изменение означает более медленные ответы. Нужно ≥{cfg["min_period_samples"]} ответов в каждом из двух соседних месяцев. Это описательное сравнение, не проверка статистической значимости.</p>')
    periods = data['periods']
    for kind, name in [('daily', 'По дням'), ('weekly', 'По неделям'), ('monthly', 'По месяцам'), ('weekday', 'По дням недели'), ('hour', 'По часу начала ожидания')]:
        rows = periods[kind]
        if kind == 'weekday':
            rows = [dict(r, period=['Пн','Вт','Ср','Чт','Пт','Сб','Вс'][int(r['period'])]) for r in rows]
        body.append(f'<h2>{name}</h2>')
        body.append(chart(rows, 'median_min', 'Типичная задержка', 'Медиана, минуты без сна'))
        if kind in ('daily', 'monthly'):
            body.append(chart(rows, 'mean_min', 'Средняя задержка', 'Среднее, минуты без сна'))
            body.append(chart(rows, 'messages', 'Активность переписки', 'Количество сообщений'))
        body.append('<details><summary>Все значения и размер выборки</summary>' + table(
            ['Период', 'Вы: ответов', 'Медиана, мин', 'Среднее, мин', 'Друг: ответов', 'Медиана, мин', 'Среднее, мин'],
            [[r['period'], r['me']['n'], fmt(r['me']['median_min']), fmt(r['me']['mean_min']), r['friend']['n'], fmt(r['friend']['median_min']), fmt(r['friend']['mean_min'])] for r in rows]) + '</details>')
    body.append('<h2>Долгие паузы — отдельный расчёт</h2>')
    body.append(table(['Кто', 'Ответов с долгими паузами', 'Среднее, мин', 'Медиана, мин'],
        [[LABELS[r], summary[r]['including_long_gaps']['n'], fmt(summary[r]['including_long_gaps']['mean_min']), fmt(summary[r]['including_long_gaps']['median_min'])] for r in ROLES]))
    body.append('<p>Тексты, имена, username, номера телефонов, идентификаторы аккаунтов и медиа в отчёт не включены. Точные даты и время переписки остаются в данных. Наведите на точку графика для значения. JSON содержит исходные временные метки, все переходы и настройки для повторного анализа.</p>')
    return '''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Статистика ответов</title><style>
body{font:16px/1.6 system-ui,sans-serif;background:#f3f6fa;color:#17243b;margin:0}main{max-width:1100px;margin:auto;background:white;padding:clamp(18px,4vw,48px)}h1{font-size:clamp(26px,5vw,40px);line-height:1.2}h2{margin-top:45px}h3{margin-bottom:0}.muted{color:#64748b;font-size:14px}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:left;padding:10px;border-bottom:1px solid #e2e8f0;white-space:nowrap}th{background:#eff4fa}svg{width:100%;display:block}svg text{font:11px system-ui;fill:#64748b}details{margin:18px 0}summary{cursor:pointer;color:#2563eb}li{margin:8px 0}
</style><main>''' + ''.join(body) + '</main></html>'


def write_outputs(messages, cfg, start, end, out, demo=False):
    out.mkdir(parents=True, exist_ok=True)
    data = analyze(messages, cfg, parse_dt(end))
    payload = {'schema_version': 1, 'demo': demo, 'generated_at': datetime.now(UTC).isoformat(),
               'start': start, 'end': end, 'config': cfg,
               'method': 'Alternating sender blocks; latency from first and last prompt; sleep excluded for responder; calendar bins by first prompt in report_timezone; long gaps excluded from main metrics.',
               'messages': messages, 'analysis': data}
    (out/'stats.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    (out/'report.html').write_text(render_report(payload), encoding='utf-8')
    for kind, rows in data['periods'].items():
        flat = []
        for row in rows:
            for role in ROLES:
                flat.append({'period': row['period'], 'who': role, **row[role]})
        write_csv(out/f'{kind}.csv', flat)
    write_csv(out/'responses.csv', data['events'])
    (out/'summary.txt').write_text('\n'.join(f'{LABELS[r]}: {data["summary"][r]["n"]} ответов; медиана {fmt(data["summary"][r]["median_min"])} мин; среднее {fmt(data["summary"][r]["mean_min"])} мин без ночи.' for r in ROLES), encoding='utf-8')
    archive = out/'telegram_stats.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
        for name in ['stats.json', 'report.html', 'summary.txt', 'responses.csv'] + [k+'.csv' for k in data['periods']]:
            z.write(out/name, name)
    print((out/'summary.txt').read_text(encoding='utf-8'))
    print('Готово:', archive.resolve())
    return archive


def write_csv(path, rows):
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        if rows:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)


def demo_messages():
    rng = random.Random(42)
    result = []
    start = datetime(2026, 1, 1, 4, tzinfo=UTC)
    for day in range(150):
        t = start + timedelta(days=day)
        role = rng.choice(ROLES)
        for _ in range(rng.randint(6, 20)):
            role = 'friend' if role == 'me' else 'me'
            delay = rng.uniform(20, 300)
            if role == 'friend' and 50 <= day < 90:
                delay *= 8
            if role == 'me' and day >= 100:
                delay *= 5
            t += timedelta(seconds=delay)
            result.append({'ts': t.isoformat(), 'who': role, 'kind': 'text', 'seq': len(result)})
    return result


def bounds(args, cfg):
    zone = ZoneInfo(cfg['report_timezone'])
    now = datetime.now(UTC)
    start = datetime.combine(datetime.fromisoformat(args.since).date(), time(), zone).astimezone(UTC) if args.since else datetime(2013, 1, 1, tzinfo=UTC)
    end = datetime.combine(datetime.fromisoformat(args.until).date() + timedelta(days=1), time(), zone).astimezone(UTC) if args.until else now
    end = min(end, now)
    if start >= end:
        raise ValueError('Начало периода должно быть раньше конца')
    return start, end


async def collect(args, cfg):
    try:
        from telethon import TelegramClient, errors, types
    except ImportError:
        raise ValueError('Установите зависимости: python -m pip install -r requirements.txt')
    api_id = os.environ.get('TG_API_ID') or input('api_id с my.telegram.org: ').strip()
    api_hash = os.environ.get('TG_API_HASH') or getpass.getpass('api_hash (ввод скрыт): ').strip()
    start, end = bounds(args, cfg)
    session = Path(__file__).resolve().parent/'private'/'account'
    session.parent.mkdir(mode=0o700, exist_ok=True)
    client = TelegramClient(str(session), int(api_id), api_hash, flood_sleep_threshold=60,
                            receive_updates=False)
    if args.proxy:
        import socks
        host, sep, port = args.proxy.rpartition(':')
        if not sep or not host or not port.isdigit():
            raise ValueError('--proxy должен иметь формат HOST:PORT')
        client.set_proxy((socks.SOCKS5, host, int(port)))
    try:
        await client.start()  # Phone, login code and 2FA password are requested locally.
        me = await client.get_me()
        if args.chat:
            target = int(args.chat) if args.chat.lstrip('-').isdigit() else args.chat
            entity = await client.get_entity(target)
        else:
            choices = []
            print('\nЛичные чаты:')
            async for dialog in client.iter_dialogs():
                if dialog.is_user and not dialog.entity.bot and dialog.id != me.id:
                    choices.append(dialog.entity)
                    print(f'{len(choices)}. {dialog.name}')
            if not choices:
                raise ValueError('Личных чатов не найдено')
            number = int(input('Номер чата: '))
            if not 1 <= number <= len(choices):
                raise ValueError('Неверный номер чата')
            entity = choices[number-1]
        if not isinstance(entity, types.User) or entity.bot or entity.id == me.id:
            raise ValueError('Выберите личный чат с другим человеком')
        print('Читаю историю. Медиа и тексты не скачиваются. Ctrl+C — остановить.')
        messages = []
        async for m in client.iter_messages(entity, limit=None, offset_date=end, wait_time=1):
            if m.date < start:
                break
            if m.date >= end or m.action or m.sender_id not in (me.id, entity.id):
                continue
            kind = ('voice' if m.voice else 'video_note' if m.video_note else
                    'photo' if m.photo else 'video' if m.video else
                    'sticker' if m.sticker else 'file' if m.document else
                    'text' if not m.media else 'other')
            messages.append({'ts': m.date.astimezone(UTC).isoformat(),
                             'who': 'me' if m.sender_id == me.id else 'friend',
                             'kind': kind, 'seq': m.id})
            if len(messages) % 1000 == 0:
                print('Прочитано сообщений:', len(messages))
        # Keep stable timestamp ties without exporting Telegram message IDs.
        messages.sort(key=lambda m: (m['ts'], m['seq']))
        for i, m in enumerate(messages):
            m['seq'] = i
        archive = write_outputs(messages, cfg, start.isoformat(), end.isoformat(), args.output)
        if args.saved:
            await client.send_file('me', str(archive), force_document=True,
                                   caption='Статистика задержек ответов. report.html — графики; stats.json — данные для анализа. Текстов сообщений нет.')
            print('Архив отправлен в Избранное.')
    except errors.FloodWaitError as e:
        raise ValueError(f'Telegram просит подождать {e.seconds} секунд. Повторите позже.')
    finally:
        await client.disconnect()


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser(description='Статистика ответов в личном чате Telegram')
    p.add_argument('--config', type=Path, default=Path(__file__).with_name('config.json'))
    p.add_argument('--chat', help='username или известный ID; без параметра — выбор из списка')
    p.add_argument('--since', help='С даты YYYY-MM-DD включительно; по умолчанию вся доступная история')
    p.add_argument('--until', help='До даты YYYY-MM-DD включительно; по умолчанию до запуска')
    p.add_argument('--saved', action='store_true', help='Отправить готовый ZIP себе в Избранное')
    p.add_argument('--output', type=Path, default=Path('output'))
    p.add_argument('--proxy', help='Необязательный SOCKS5 без авторизации HOST:PORT')
    p.add_argument('--demo', action='store_true', help='Проверка на синтетических данных без Telegram')
    p.add_argument('--reanalyze', type=Path, help='Пересчитать stats.json с текущим config.json, без Telegram')
    args = p.parse_args()
    try:
        cfg = load_config(args.config)
        if args.demo:
            messages = demo_messages()
            write_outputs(messages, cfg, messages[0]['ts'], messages[-1]['ts'], args.output, demo=True)
        elif args.reanalyze:
            old = json.loads(args.reanalyze.read_text(encoding='utf-8'))
            write_outputs(old['messages'], cfg, old['start'], old['end'], args.output, old.get('demo', False))
        else:
            asyncio.run(collect(args, cfg))
    except KeyboardInterrupt:
        print('\nОстановлено.', file=sys.stderr)
        return 130
    except Exception as e:
        print(f'Ошибка: {e}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
