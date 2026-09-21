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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("broadcast")

MODE_LABELS = {
    "everyone": "@everyone",
    "here": "@here",
    "members": "members (全員を個別メンション)",
    "role": "role (ロールメンション)",
    "none": "none (メンションなし)",
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
        header += [
            f"送信回数: {self.count} 回",
            f"合計メッセージ数: {self.total_messages}",
            f"送信間隔: {self.delay} 秒",
        ]

        estimated = self.total_messages * self.delay
        if estimated >= 600:
            header.append(
                f"⚠️ 推定所要時間: 約 {estimated / 60:.0f} 分"
                " (長い処理のため、進捗表示が途中で止まる場合があります)"
            )

        header += ["", "本文:"]

        footer: list[str] = []
        if self.mention_sample:
            footer = ["", f"メンションの例: {self.mention_sample}"]

        room = PREVIEW_LIMIT - len("\n".join(header)) - len("\n".join(footer)) - 20
        body = self.message
        if len(body) > room:
            body = body[: max(0, room - 8)] + " …(以下略)"

        return "\n".join(header + [body] + footer)


async def run_plan(
    plan: BroadcastPlan,
    progress: Optional[Callable[[str], Awaitable[None]]] = None,
) -> tuple[int, list[str], float]:
    """plan を実行する。(送信数, エラー一覧, 所要秒) を返す。"""
    sent = 0
    errors: list[str] = []
    started = time.monotonic()

    for round_index in range(1, plan.count + 1):
        for payload in plan.payloads:
            try:
                await plan.channel.send(payload, allowed_mentions=plan.allowed_mentions)
            except discord.Forbidden:
                errors.append("送信権限がありません (チャンネル権限と Bot のロールを確認してください)")
                break
            except discord.HTTPException as exc:
                errors.append(f"送信に失敗しました: {exc.status} {exc.text}")
                break
            sent += 1
            if plan.delay > 0:
                await asyncio.sleep(plan.delay)

        if errors:
            break
        if progress is not None:
            await progress(
                f"⏳ 送信中… {sent}/{plan.total_messages} ({round_index}/{plan.count} 回目)"
            )

    return sent, errors, time.monotonic() - started


def summary_text(plan: BroadcastPlan, sent: int, errors: list[str], elapsed: float) -> str:
    lines = [
        "✅ 送信が完了しました。" if not errors else "⚠️ 途中で停止しました。",
        f"メンション: {plan.mode_label} / 回数: {plan.count} 回",
        f"送信数: {sent}/{plan.total_messages}",
        f"送信先: {plan.channel_label}",
        f"所要時間: {elapsed:.1f} 秒",
    ]
    if plan.target_info:
        lines.append(plan.target_info)
    if errors:
        lines.append("エラー: " + " / ".join(dict.fromkeys(errors)))
    return "\n".join(lines)


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
        await interaction.response.edit_message(view=self)
        sent, errors, elapsed = await run_plan(
            self.plan, progress=lambda text: self.edit_status(interaction, text)
        )
        await self.edit_status(interaction, summary_text(self.plan, sent, errors, elapsed))

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
    include_bots: bool,
) -> tuple[Optional[BroadcastPlan], Optional[str]]:
    """(plan, エラーメッセージ) を返す。エラー時は plan が None。"""
    channel = interaction.channel
    if interaction.guild is None or not isinstance(channel, discord.abc.Messageable):
        return None, "サーバー内のテキストチャンネルで実行してください。"

    if len(message) > MAX_CONTENT_LEN:
        return None, f"本文が長すぎます ({len(message)} 文字 / 上限 {MAX_CONTENT_LEN} 文字)。"

    channel_label = getattr(channel, "mention", str(channel))
    mode_label = MODE_LABELS[mode]
    mention_chunks: list[str] = []
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
        members = await collect_members(interaction.guild, include_bots, filter_role)
        if not members:
            return None, "メンション対象のメンバーが見つかりませんでした。"
        mention_chunks = chunk_mentions([member.id for member in members], message)
        target_info = f"対象メンバー: {len(members)} 人 / 1 回あたり {len(mention_chunks)} メッセージ"
        if filter_role is not None:
            target_info += f" (ロール {filter_role.name} のみ)"
        sample = " ".join(member.mention for member in members[:5])
        if len(members) > 5:
            sample += f" … ほか {len(members) - 5} 人"
        mention_sample = sample

    payloads = (
        [message]
        if not mention_chunks
        else [f"{chunk}\n{message}" for chunk in mention_chunks]
    )

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
            total_messages=count * len(payloads),
            allowed_mentions=ALLOWED_MENTIONS[mode],
            target_info=target_info,
            mention_sample=mention_sample,
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
    app_commands.Choice(name="members (全員を個別メンション)", value="members"),
    app_commands.Choice(name="role (ロールメンション)", value="role"),
    app_commands.Choice(name="none (メンションなし)", value="none"),
]


@bot.tree.command(
    name="broadcast",
    description="カスタム文面を指定回数送信します (送信前に確認ボタンが出ます)",
)
@app_commands.describe(
    message="送信する本文",
    count="送信する回数",
    target="メンションの対象",
    role="target=role のときにメンションするロール",
    filter_role="target=members のときに、このロールを持つ人だけに送る",
    delay="送信ごとの待機秒数 (レート制限対策)",
    include_bots="target=members のときに Bot も含める",
    dry_run="確認ボタンを出さずに内容だけ確認する",
)
@app_commands.choices(target=TARGET_CHOICES)
@app_commands.default_permissions(manage_guild=True)
async def broadcast(
    interaction: discord.Interaction,
    message: str,
    count: app_commands.Range[int, 1, MAX_COUNT] = 1,
    target: Optional[app_commands.Choice[str]] = None,
    role: Optional[discord.Role] = None,
    filter_role: Optional[discord.Role] = None,
    delay: app_commands.Range[float, 0.0, 30.0] = 1.5,
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
    plan, error = await build_plan(
        interaction, message, count, mode, role, filter_role, delay, include_bots
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
