"""Discord broadcast bot — /broadcast スラッシュコマンド

カスタム文面を指定回数チャンネルへ送信する。target=members を選ぶと
サーバーメンバーを 1 人ずつ <@id> でメンションするため、@everyone / @here を
抑制しているメンバーにも通知が届く。

送信前に確認ボタン (送信する / キャンセル) を挟むので、誤爆しない。

環境変数:
    DISCORD_TOKEN     Bot トークン (必須)
    ALLOWED_GUILD_IDS コマンドを即時反映させたいギルド ID (カンマ区切り, 任意)
    MAX_COUNT         1 回のコマンドで送信できる最大回数 (既定 20)
    PORT              Render などで Web Service として動かすときのポート (任意)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Sequence

import discord
from discord import app_commands
from discord.ext import commands

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "").strip()
ALLOWED_GUILD_IDS = {
    int(value)
    for value in os.environ.get("ALLOWED_GUILD_IDS", "").replace(" ", "").split(",")
    if value.isdigit()
}
MAX_COUNT = max(1, int(os.environ.get("MAX_COUNT", "20")))
PORT = int(os.environ["PORT"]) if os.environ.get("PORT", "").isdigit() else None

# 1 メッセージの上限は 2000 文字。安全マージンを取る。
MAX_CONTENT_LEN = 1900
MIN_MENTION_BUDGET = 25
PREVIEW_LIMIT = 1850
CONFIRM_TIMEOUT = 180
MAX_INTERVAL = 3600.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("broadcast")

MODE_LABELS = {
    "everyone": "@everyone",
    "here": "@here",
    "members": "members (メンバーを個別メンション)",
    "role": "role (ロールメンション)",
    "none": "none (メンションなし)",
}

SEND_STYLE_LABELS = {
    "chunked": "chunked (1 メッセージにまとめて)",
    "per_member": "per_member (1 人ずつ別々に送信)",
}

ALLOWED_MENTIONS = {
    "everyone": discord.AllowedMentions(everyone=True),
    "here": discord.AllowedMentions(everyone=True),
    "role": discord.AllowedMentions(roles=True),
    "members": discord.AllowedMentions(users=True),
    "none": discord.AllowedMentions.none(),
}

# プレビューや進捗表示で大量メンションが誤爆しないようにする
QUIET_MENTIONS = discord.AllowedMentions.none()


def chunk_mentions(user_ids: Sequence[int], message: str) -> list[str]:
    """<@id> の並びを 1 メッセージに収まる長さへ分割する。"""
    budget = MAX_CONTENT_LEN - len(message) - 1  # 本文との間の改行 1 文字分
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for user_id in user_ids:
        piece = f"<@{user_id}> "
        if current and current_len + len(piece) > budget:
            chunks.append("".join(current).rstrip())
            current = []
            current_len = 0
        current.append(piece)
        current_len += len(piece)

    if current:
        chunks.append("".join(current).rstrip())
    return chunks


async def collect_members(
    guild: discord.Guild,
    include_bots: bool,
    filter_role: Optional[discord.Role],
) -> list[discord.Member]:
    """ギルドのメンバー一覧を取得する (キャッシュが不完全なら gateway から取得)。"""
    found: dict[int, discord.Member] = {}
    expected = guild.member_count or 0

    if len(guild.members) < expected:
        try:
            async for member in guild.fetch_members(limit=None):
                found[member.id] = member
        except discord.HTTPException as exc:
            log.warning("fetch_members failed: %s", exc)

    if len(found) < expected:
        for member in guild.members:
            found[member.id] = member

    members = [member for member in found.values() if include_bots or not member.bot]
    if filter_role is not None:
        members = [member for member in members if filter_role in member.roles]
    members.sort(key=lambda member: member.id)
    return members


class StopFlag:
    """連続送信を途中で止めるためのフラグ。"""

    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


async def _sleep(seconds: float, stop: Optional[StopFlag] = None) -> None:
    """停止要求に素早く反応できるよう、細かく分けて待機する。"""
    remaining = float(seconds)
    while remaining > 0:
        if stop is not None and stop.stopped:
            return
        step = min(1.0, remaining)
        await asyncio.sleep(step)
        remaining -= step


def resolve_members(
    pool: Sequence[discord.Member],
    raw: str,
) -> tuple[list[discord.Member], list[str]]:
    """ID / @メンション / ユーザー名 / 表示名 から対象メンバーを特定する。"""
    by_id = {str(member.id): member for member in pool}
    by_name: dict[str, discord.Member] = {}
    for member in pool:
        keys = (
            member.name,
            member.display_name,
            f"{member.name}#{member.discriminator}",
        )
        for key in keys:
            by_name.setdefault(key.casefold(), member)

    tokens = [token.strip() for token in raw.replace("、", ",").replace("，", ",").split(",")]
    tokens = [token for token in tokens if token]

    if len(tokens) == 1:
        whole = tokens[0].casefold()
        if whole in by_name:
            # 空白を含む表示名をそのまま書けるようにする
            return [by_name[whole]], []

    resolved: list[discord.Member] = []
    unresolved: list[str] = []
    for token in tokens:
        member: Optional[discord.Member] = None
        if token.startswith("<@") and token.endswith(">"):
            member = by_id.get(token[2:-1].lstrip("!"))
        if member is None:
            member = by_id.get(token) or by_name.get(token.casefold())
        if member is None:
            unresolved.append(token)
        elif member not in resolved:
            resolved.append(member)
    return resolved, unresolved


def human_seconds(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} 秒"
    if seconds < 3600:
        return f"{seconds / 60:.0f} 分"
    return f"{seconds / 3600:.1f} 時間"


def permission_report(interaction: discord.Interaction) -> tuple[str, str]:
    """(Bot の権限の内訳, 不足している権限) を返す。判定できないときは ("", "")。"""
    guild = interaction.guild
    channel = interaction.channel
    if guild is None or not isinstance(channel, discord.abc.GuildChannel):
        return "", ""
    me = guild.me
    if me is None:
        return "", ""

    perms = channel.permissions_for(me)
    checks = (
        ("チャンネルを表示", perms.view_channel),
        ("メッセージを送る", perms.send_messages),
        ("全員宛にメンション", perms.mention_everyone),
    )
    missing = [label for label, ok in checks if not ok]
    info = "Bot の権限: " + " / ".join(
        f"{label}={'あり' if ok else 'なし'}" for label, ok in checks
    )
    return info, "、".join(missing)


@dataclass
class BroadcastPlan:
    """送信内容の確定版。確認ボタンではこれを表示してから実行する。"""

    channel: discord.abc.Messageable
    channel_label: str
    mode: str
    mode_label: str
    message: str
    payloads: list[str]
    count: int
    delay: float
    total_messages: int
    allowed_mentions: discord.AllowedMentions
    target_info: str = ""
    mention_sample: str = ""
    interval: float = 0.0
    send_style: str = "chunked"
    permission_info: str = ""
    missing_perms: str = ""

    def render(self) -> str:
        """確認用テキストを組み立てる (2000 文字を超えないよう本文を切り詰める)。"""
        header = [
            "📝 送信内容の確認 (この時点ではまだ送信していません)",
            "",
            f"送信先: {self.channel_label}",
            f"メンション: {self.mode_label}",
        ]
        if self.target_info:
            header.append(self.target_info)
        if self.missing_perms:
            header.append(
                f"⚠️ Bot に「{self.missing_perms}」の権限がありません。このままでは送信に失敗します。"
            )
        header += [
            f"送信方法: {SEND_STYLE_LABELS.get(self.send_style, self.send_style)}",
            f"送信回数: {self.count} 回",
            f"合計メッセージ数: {self.total_messages}",
            f"メッセージ間の待機: {self.delay} 秒",
        ]
        if self.interval > 0 and self.count > 1:
            header.append(f"回と回の間隔: {self.interval} 秒")

        estimated = self.total_messages * self.delay + max(0, self.count - 1) * self.interval
        if estimated >= 300:
            header.append(
                f"⏱️ 推定所要時間: 約 {human_seconds(estimated)}"
                " (長い処理のため、進捗表示が途中で止まる場合があります)"
            )
        if self.total_messages >= 200:
            header.append("⚠️ 送信数が多いため時間がかかります。送信中は「停止」で中断できます。")

        header += ["", "本文:"]

        footer: list[str] = []
        if self.mention_sample:
            footer = ["", f"メンションの例: {self.mention_sample}"]

        room = PREVIEW_LIMIT - len("\n".join(header)) - len("\n".join(footer)) - 20
        body = self.message
        if len(body) > room:
            body = body[: max(0, room - 8)] + " …(以下略)"

        return "\n".join(header + [body] + footer)


def progress_text(plan: BroadcastPlan, sent: int, round_index: int) -> str:
    return (
        f"⏳ 送信中… {sent}/{plan.total_messages} ({round_index}/{plan.count} 回目)"
        " / 中断したいときは「停止」を押してください"
    )


async def run_plan(
    plan: BroadcastPlan,
    progress: Optional[Callable[[str], Awaitable[None]]] = None,
    stop: Optional[StopFlag] = None,
) -> tuple[int, list[str], bool, float]:
    """plan を実行する。(送信数, エラー一覧, 停止されたか, 所要秒) を返す。"""
    sent = 0
    errors: list[str] = []
    stopped = False
    started = time.monotonic()
    last_progress = started

    for round_index in range(1, plan.count + 1):
        if stop is not None and stop.stopped:
            stopped = True
            break

        for payload in plan.payloads:
            if stop is not None and stop.stopped:
                stopped = True
                break
            try:
                await plan.channel.send(payload, allowed_mentions=plan.allowed_mentions)
            except discord.Forbidden:
                errors.append("送信権限がありません (チャンネル権限と Bot のロールを確認してください)")
                break
            except discord.HTTPException as exc:
                errors.append(f"送信に失敗しました: {exc.status} {exc.text}")
                break

            sent += 1
            await _sleep(plan.delay, stop)

            if progress is not None and time.monotonic() - last_progress >= 3.0:
                last_progress = time.monotonic()
                await progress(progress_text(plan, sent, round_index))

        if errors or stopped:
            break

        if progress is not None:
            await progress(progress_text(plan, sent, round_index))

        if round_index < plan.count and plan.interval > 0:
            await _sleep(plan.interval, stop)

    return sent, errors, stopped, time.monotonic() - started


def summary_text(
    plan: BroadcastPlan,
    sent: int,
    errors: list[str],
    stopped: bool,
    elapsed: float,
) -> str:
    if errors:
        head = "⚠️ 途中で停止しました。"
    elif stopped:
        head = "⏹️ 停止しました (残りは送信していません)。"
    else:
        head = "✅ 送信が完了しました。"

    lines = [
        head,
        f"メンション: {plan.mode_label} / 回数: {plan.count} 回",
        f"送信数: {sent}/{plan.total_messages}",
        f"送信先: {plan.channel_label}",
        f"所要時間: {elapsed:.1f} 秒",
    ]
    if plan.target_info:
        lines.append(plan.target_info)
    if errors:
        lines.append("エラー: " + " / ".join(dict.fromkeys(errors)))
        if plan.permission_info:
            lines.append(plan.permission_info)
            lines.append(
                "対処: サーバー設定 > ロール で Bot のロールに「メッセージを送る」を許可するか、"
                "対象チャンネルの権限で Bot に許可してください。"
            )
    return "\n".join(lines)


class StopView(discord.ui.View):
    """連続送信中に出す停止ボタン。"""

    def __init__(self, flag: StopFlag, author_id: int) -> None:
        super().__init__(timeout=None)
        self.flag = flag
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "このボタンはコマンドを実行した本人だけが使えます。", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="停止", style=discord.ButtonStyle.secondary)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.flag.stop()
        button.disabled = True
        await interaction.response.edit_message(view=self)


class ConfirmView(discord.ui.View):
    """送信前に出す確認ボタン。押せるのはコマンドを実行した本人だけ。"""

    def __init__(self, plan: BroadcastPlan, author_id: int) -> None:
        super().__init__(timeout=CONFIRM_TIMEOUT)
        self.plan = plan
        self.author_id = author_id
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "このボタンはコマンドを実行した本人だけが使えます。", ephemeral=True
            )
            return False
        return True

    def disable_all(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    async def on_timeout(self) -> None:
        self.disable_all()
        if self.message is None:
            return
        try:
            await self.message.edit(
                content="⌛ 確認待ちの時間切れです。何も送信していません。", view=self
            )
        except discord.HTTPException:
            pass

    async def edit_status(self, interaction: discord.Interaction, content: str) -> None:
        try:
            await interaction.edit_original_response(
                content=content, allowed_mentions=QUIET_MENTIONS
            )
        except discord.HTTPException as exc:
            log.warning("status edit failed: %s", exc)

    @discord.ui.button(label="送信する", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.disable_all()
        self.stop()  # 送信中のタイムアウト処理を止める (進捗表示の上書き防止)
        flag = StopFlag()
        await interaction.response.edit_message(view=StopView(flag, self.author_id))
        sent, errors, stopped, elapsed = await run_plan(
            self.plan,
            progress=lambda text: self.edit_status(interaction, text),
            stop=flag,
        )
        try:
            await interaction.edit_original_response(
                content=summary_text(self.plan, sent, errors, stopped, elapsed),
                view=None,
                allowed_mentions=QUIET_MENTIONS,
            )
        except discord.HTTPException as exc:
            log.warning("summary edit failed: %s", exc)

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.disable_all()
        self.stop()
        await interaction.response.edit_message(
            content="キャンセルしました。何も送信していません。", view=self
        )


async def build_plan(
    interaction: discord.Interaction,
    message: str,
    count: int,
    mode: str,
    role: Optional[discord.Role],
    filter_role: Optional[discord.Role],
    delay: float,
    interval: float,
    include_bots: bool,
    users: Optional[str],
    send_style: str,
) -> tuple[Optional[BroadcastPlan], Optional[str]]:
    """(plan, エラーメッセージ) を返す。エラー時は plan が None。"""
    channel = interaction.channel
    if interaction.guild is None or not isinstance(channel, discord.abc.Messageable):
        return None, "サーバー内のテキストチャンネルで実行してください。"

    if len(message) > MAX_CONTENT_LEN:
        return None, f"本文が長すぎます ({len(message)} 文字 / 上限 {MAX_CONTENT_LEN} 文字)。"

    permission_info, missing_perms = permission_report(interaction)

    if users:
        # users が指定されたときは個別メンションに切り替える
        mode = "members"

    if send_style == "per_member" and mode != "members":
        return None, (
            "send_style=per_member (1 人ずつ送信) は target=members か users と組み合わせてください。"
        )

    channel_label = getattr(channel, "mention", str(channel))
    mode_label = MODE_LABELS[mode]
    mention_chunks: list[str] = []
    member_list: list[discord.Member] = []
    member_ids: list[int] = []
    unresolved: list[str] = []
    target_info = ""
    mention_sample = ""

    if mode == "everyone":
        mention_chunks = ["@everyone"]
    elif mode == "here":
        mention_chunks = ["@here"]
    elif mode == "role":
        if role is None:
            return None, "target=role を選んだ場合は role も指定してください。"
        mention_chunks = [role.mention]
        mode_label = f"role ({role.name})"
    elif mode == "members":
        if MAX_CONTENT_LEN - len(message) - 1 < MIN_MENTION_BUDGET:
            return None, (
                "target=members のときは本文を "
                f"{MAX_CONTENT_LEN - MIN_MENTION_BUDGET} 文字以内にしてください。"
            )
        pool = await collect_members(interaction.guild, include_bots, filter_role)
        if users:
            pool, unresolved = resolve_members(pool, users)
            if not pool:
                return None, (
                    "users で指定したメンバーが見つかりませんでした: " + ", ".join(unresolved)
                )
        if not pool:
            return None, "メンション対象のメンバーが見つかりませんでした。"

        member_list = pool
        member_ids = [member.id for member in pool]

        notes: list[str] = []
        if users:
            notes.append("users で指定")
        if filter_role is not None:
            notes.append(f"ロール {filter_role.name} のみ")
        target_info = f"対象メンバー: {len(member_ids)} 人"
        if notes:
            target_info += " (" + " / ".join(notes) + ")"

        sample = " ".join(member.mention for member in member_list[:5])
        if len(member_list) > 5:
            sample += f" … ほか {len(member_list) - 5} 人"
        mention_sample = sample

    if send_style == "per_member":
        payloads = [f"<@{user_id}>\n{message}" for user_id in member_ids]
        target_info += f" / 1 回あたり {len(payloads)} メッセージ"
    else:
        if member_ids:
            mention_chunks = chunk_mentions(member_ids, message)
            target_info += f" / 1 回あたり {len(mention_chunks)} メッセージ"
        payloads = (
            [message]
            if not mention_chunks
            else [f"{chunk}\n{message}" for chunk in mention_chunks]
        )

    if unresolved:
        target_info += f" / 未解決: {', '.join(unresolved)}"

    return (
        BroadcastPlan(
            channel=channel,
            channel_label=channel_label,
            mode=mode,
            mode_label=mode_label,
            message=message,
            payloads=payloads,
            count=count,
            delay=delay,
            interval=interval,
            send_style=send_style,
            total_messages=count * len(payloads),
            allowed_mentions=ALLOWED_MENTIONS[mode],
            target_info=target_info,
            mention_sample=mention_sample,
            permission_info=permission_info,
            missing_perms=missing_perms,
        ),
        None,
    )


class BroadcastBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True  # メンバー一覧の取得に必須 (Portal 側の設定も必要)
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            allowed_mentions=QUIET_MENTIONS,
        )

    async def setup_hook(self) -> None:
        if PORT is not None:
            self.loop.create_task(self._serve_health())

        if ALLOWED_GUILD_IDS:
            for guild_id in sorted(ALLOWED_GUILD_IDS):
                guild = discord.Object(id=guild_id)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info("synced %d commands to guild %s", len(synced), guild_id)
        else:
            synced = await self.tree.sync()
            log.info("synced %d global commands (反映に最大 1 時間)", len(synced))

    async def _serve_health(self) -> None:
        """Render の Web Service として動かすための最小 HTTP サーバー。"""
        from aiohttp import web

        async def handle(_request: web.Request) -> web.Response:
            return web.Response(text="ok")

        app = web.Application()
        app.router.add_get("/", handle)
        app.router.add_get("/health", handle)

        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()
        log.info("health server listening on 0.0.0.0:%s", PORT)

    async def on_ready(self) -> None:
        log.info("logged in as %s (guilds=%d)", self.user, len(self.guilds))


bot = BroadcastBot()

TARGET_CHOICES = [
    app_commands.Choice(name="everyone (@everyone)", value="everyone"),
    app_commands.Choice(name="here (@here)", value="here"),
    app_commands.Choice(name="members (メンバーを個別メンション)", value="members"),
    app_commands.Choice(name="role (ロールメンション)", value="role"),
    app_commands.Choice(name="none (メンションなし)", value="none"),
]

SEND_STYLE_CHOICES = [
    app_commands.Choice(name="chunked (1 メッセージにまとめて)", value="chunked"),
    app_commands.Choice(name="per_member (1 人ずつ別々に送信)", value="per_member"),
]


@bot.tree.command(
    name="broadcast",
    description="メンション付きの文面を指定回数・指定間隔で送信します (確認ボタン付き)",
)
@app_commands.describe(
    message="送信する本文",
    count="送信する回数",
    target="メンションの対象",
    users="個別にメンションする相手 (カンマ区切りの ID / @メンション / 名前)。指定すると個別メンションになります",
    role="target=role のときにメンションするロール",
    filter_role="target=members のときに、このロールを持つ人だけに送る",
    send_style="members の送り方: まとめて / 1 人ずつ",
    delay="1 メッセージごとの待機秒数 (レート制限対策)",
    interval="回と回の間隔 (秒)。連続送信したいときに指定します",
    include_bots="対象に Bot も含める",
    dry_run="確認ボタンを出さずに内容だけ確認する",
)
@app_commands.choices(target=TARGET_CHOICES, send_style=SEND_STYLE_CHOICES)
@app_commands.default_permissions(manage_guild=True)
async def broadcast(
    interaction: discord.Interaction,
    message: str,
    count: app_commands.Range[int, 1, MAX_COUNT] = 1,
    target: Optional[app_commands.Choice[str]] = None,
    users: Optional[str] = None,
    role: Optional[discord.Role] = None,
    filter_role: Optional[discord.Role] = None,
    send_style: Optional[app_commands.Choice[str]] = None,
    delay: app_commands.Range[float, 0.0, 30.0] = 1.5,
    interval: app_commands.Range[float, 0.0, MAX_INTERVAL] = 0.0,
    include_bots: bool = False,
    dry_run: bool = False,
) -> None:
    if interaction.guild is None or not isinstance(interaction.channel, discord.abc.Messageable):
        await interaction.response.send_message(
            "サーバー内のテキストチャンネルで実行してください。", ephemeral=True
        )
        return

    permissions = interaction.permissions
    if not (
        permissions.manage_guild
        or permissions.administrator
        or permissions.mention_everyone
    ):
        await interaction.response.send_message(
            "実行するにはサーバー管理またはメンション @everyone の権限が必要です。",
            ephemeral=True,
        )
        return

    if len(message) > MAX_CONTENT_LEN:
        await interaction.response.send_message(
            f"本文が長すぎます ({len(message)} 文字 / 上限 {MAX_CONTENT_LEN} 文字)。",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    mode = target.value if target is not None else "everyone"
    style = send_style.value if send_style is not None else "chunked"
    plan, error = await build_plan(
        interaction,
        message,
        count,
        mode,
        role,
        filter_role,
        delay,
        interval,
        include_bots,
        users,
        style,
    )
    if plan is None:
        await interaction.followup.send(f"⚠️ {error}", ephemeral=True)
        return

    preview = plan.render()

    if dry_run:
        await interaction.followup.send(
            preview + "\n\n(dry_run のため送信していません)",
            ephemeral=True,
            allowed_mentions=QUIET_MENTIONS,
        )
        return

    view = ConfirmView(plan, interaction.user.id)
    view.message = await interaction.followup.send(
        preview + f"\n\n👉 内容を確認して、下のボタンを押してください ({CONFIRM_TIMEOUT} 秒で失効)。",
        view=view,
        ephemeral=True,
        wait=True,
        allowed_mentions=QUIET_MENTIONS,
    )


def main() -> None:
    if not DISCORD_TOKEN:
        raise SystemExit("環境変数 DISCORD_TOKEN を設定してください。")
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
