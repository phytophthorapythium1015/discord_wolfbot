import asyncio
import random
import os
from dotenv import load_dotenv
# 載入本機的 .env 檔案 (如果在 Railway 上執行，這行不會影響系統環境變數)
load_dotenv()
import discord
from discord import app_commands

# 1. 設定機器人權限 (Intents)
intents = discord.Intents.default()
intents.message_content = True


class UndercoverClient(discord.Client):

    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # 同步 Slash 指令到 Discord
        await self.tree.sync()
        print(f"已成功同步指令，目前登入身分：{self.user}")


client = UndercoverClient()

# 遊戲狀態儲存字典 (以 Channel ID 作為 Key)
games = {}


class GameSession:

    def __init__(self, channel: discord.TextChannel, host: discord.Member):
        self.channel = channel
        self.host = host
        self.players = []  # 參與玩家列表 (discord.Member)
        self.words_pool = {}  # 玩家提供的詞 { player: word }
        self.secret_word = None  # 本局答案
        self.wolf = None  # 狼人 (discord.Member)
        self.state = (
            "WAITING"  # WAITING, COLLECTING, CHECKING, DISCUSSING, PLAYING
        )
        self.eliminated = set()  # 已出局的玩家
        self.is_running = True  # 控制遊戲迴圈是否繼續的開關
        self.vote_event = (
            asyncio.Event()
        )  # 建立控制討論暫停/喚醒的紅綠燈開關

    async def check_and_start(self, interaction: discord.Interaction):
        word_to_players = {}
        for player, word in self.words_pool.items():
            w_cleaned = word.lower()
            if w_cleaned not in word_to_players:
                word_to_players[w_cleaned] = []
            word_to_players[w_cleaned].append(player)

        duplicates = {
            word: players
            for word, players in word_to_players.items()
            if len(players) > 1
        }

        if duplicates:
            for word, players in duplicates.items():
                survivor = random.choice(players)
                for p in players:
                    if p != survivor:
                        del self.words_pool[p]
                        try:

                            class ReSubmitView(discord.ui.View):

                                def __init__(self, game):
                                    super().__init__(timeout=120)
                                    self.game = game

                                @discord.ui.button(
                                    label="重新輸入性癖",
                                    style=discord.ButtonStyle.primary,
                                )
                                async def btn_cb(
                                    self,
                                    inter: discord.Interaction,
                                    button: discord.ui.Button,
                                ):
                                    await inter.response.send_modal(
                                        ReSubmitModal(self.game)
                                    )

                            await p.send(
                                "你剛才輸入的性癖與其他玩家重複了，請點擊下方按鈕重新輸入：",
                                view=ReSubmitView(self),
                            )
                        except Exception as e:
                            print(f"無法私訊玩家 {p.display_name}: {e}")
            return

        # 沒有重複，進入討論階段
        self.state = "DISCUSSING"

        # 1. 隨機抽選答案與狼人
        all_words = list(self.words_pool.items())
        self.wolf, self.secret_word = random.choice(all_words)

        # 2. 公開狼人性癖，並宣告進入自由討論
        await self.channel.send(
            f"**公開狼人性癖：** **{self.secret_word}**"
        )

        # 3. 定義並啟動背景任務
        async def runner():
            try:
                await self.game_loop()
            except Exception as e:
                print(f"[game_loop 錯誤]: {e}")

        asyncio.create_task(runner())

    async def start_playing(self):
        self.state = "PLAYING"

        async def runner():
            try:
                await self.game_loop()
            except Exception as e:
                print(f"[game_loop 發生未預期錯誤]: {e}")
                import traceback

                traceback.print_exc()

        asyncio.create_task(runner())

    async def game_loop(self):
        round_num = 1  # 記錄當前輪數

        while self.is_running:
            # ---------------- 1. 討論階段 ----------------
            self.state = "DISCUSSING"
            self.vote_event.clear()  # 重置為未觸發狀態

            await self.channel.send(f"**第 {round_num} 輪討論開始！**")

            # 程式暫停，直到主持人輸入 /vote
            await self.vote_event.wait()

            if not self.is_running:
                break

            # ---------------- 2. 投票階段 ----------------
            self.state = "PLAYING"
            await self.channel.send(
                f"\n--- **第 {round_num} 輪投票開始** ---"
            )

            alive_players = [
                p for p in self.players if p not in self.eliminated
            ]

            view = VoteView(self, alive_players)
            msg = await self.channel.send("狼人是誰?", view=view)

            await view.wait()

            # ---------------- 3. 投票結算 ----------------
            if not view.votes:
                await self.channel.send(
                    "投票時間到，本輪無人投票，進入下一輪。"
                )
                round_num += 1
                continue

            vote_counts = {}
            for voter, target in view.votes.items():
                vote_counts[target] = vote_counts.get(target, 0) + 1

            vote_details_text = "\n".join(
                [
                    f"- **{target.display_name}**： {count} 票"
                    for target, count in vote_counts.items()
                ]
            )

            max_votes = max(vote_counts.values())
            top_candidates = [
                player
                for player, count in vote_counts.items()
                if count == max_votes
            ]

            if len(top_candidates) > 1:
                names = ", ".join([p.display_name for p in top_candidates])
                await self.channel.send(
                    f"**【投票結果】**\n{vote_details_text}\n\n"
                    f"**平手** {names} 皆 {max_votes} 票\n"
                    f"無人出局"
                )
            else:
                max_voted_target = top_candidates[0]
                await self.channel.send(
                    f"**【投票結果】**\n{vote_details_text}\n\n"
                    f"**{max_voted_target.display_name}** {max_votes} 票"
                )

                # 檢查是否投到狼人
                if max_voted_target == self.wolf:
                    await self.channel.send(
                        f"**平民獲勝！** 狼人是 {self.wolf.mention}！"
                    )
                    self.end_game()
                    break
                else:
                    self.eliminated.add(max_voted_target)
                    player_word = self.words_pool[max_voted_target]
                    await self.channel.send(
                        f"{max_voted_target.mention} 是平民，公開 {max_voted_target.mention} 性癖：**{player_word}**"
                    )

                    alive_civilians = [
                        p
                        for p in self.players
                        if p != self.wolf and p not in self.eliminated
                    ]
                    if len(alive_civilians) < 2:
                        await self.channel.send(
                            f"**狼人獲勝！** 場上剩餘平民少於 2 人。\n狼人是：{self.wolf.mention}"
                        )
                        self.end_game()
                        break

            # ---------------- 4. 狼人殺人階段 ----------------
            await self.channel.send("天黑請閉眼，狼人請殺人")
            try:
                alive_players = [
                    p for p in self.players if p not in self.eliminated
                ]
                wolf_targets = [p for p in alive_players if p != self.wolf]

                if wolf_targets:
                    options = [
                        discord.SelectOption(
                            label=p.display_name, value=str(p.id)
                        )
                        for p in wolf_targets
                    ]

                    class WolfSecretActionView(discord.ui.View):

                        def __init__(self, game):
                            super().__init__(timeout=30)
                            self.game = game
                            self.target = None

                        @discord.ui.button(
                            label="狼人點此選擇目標",
                            style=discord.ButtonStyle.danger,
                        )
                        async def secret_kill_btn(
                            self,
                            interaction: discord.Interaction,
                            button: discord.ui.Button,
                        ):
                            if interaction.user != self.game.wolf:
                                await interaction.response.send_message(
                                    "你不是狼人", ephemeral=True
                                )
                                return

                            class WolfSecretSelectView(discord.ui.View):

                                def __init__(self, outer_view):
                                    super().__init__(timeout=30)
                                    self.outer_view = outer_view
                                    self.select = discord.ui.Select(
                                        placeholder="請選擇你要殺的人",
                                        options=options,
                                    )
                                    self.select.callback = (
                                        self.select_callback
                                    )
                                    self.add_item(self.select)

                                async def select_callback(
                                    self, inter: discord.Interaction
                                ):
                                    self.outer_view.target = discord.utils.get(
                                        self.outer_view.game.players,
                                        id=int(self.select.values[0]),
                                    )
                                    await inter.response.send_message(
                                        f"你殺了 **{self.outer_view.target.display_name}**",
                                        ephemeral=True,
                                    )
                                    self.stop()
                                    self.outer_view.stop()

                            await interaction.response.send_message(
                                "請選擇你要殺的人",
                                view=WolfSecretSelectView(self),
                                ephemeral=True,
                            )

                    secret_view = WolfSecretActionView(self)
                    msg = await self.channel.send(
                        "夜幕低垂，狼人暗中行動", view=secret_view
                    )
                    await secret_view.wait()

                    if secret_view.target:
                        killed_target = secret_view.target
                        self.eliminated.add(killed_target)
                        killed_word = self.words_pool[killed_target]
                        await self.channel.send(
                            f"昨天晚上 **{killed_target.display_name}** 死了!\n公開**{killed_target.display_name}**性癖：**{killed_word}**"
                        )

                        alive_civilians = [
                            p
                            for p in self.players
                            if p != self.wolf and p not in self.eliminated
                        ]
                        if len(alive_civilians) < 2:
                            await self.channel.send(
                                f"**狼人獲勝！** 場上剩餘平民少於 2 人。\n狼人是：{self.wolf.mention}"
                            )
                            self.end_game()
                            break
                    else:
                        await self.channel.send(
                            "狼人逾時未行動，今晚為平安夜。"
                        )

            except Exception as e:
                print(f"狼人殺人階段出錯: {e}")

            round_num += 1

    def end_game(self):
        self.is_running = False
        self.vote_event.set()
        if self.channel.id in games:
            del games[self.channel.id]


# --- 介面：提供性癖的 Modal ---
class WordSubmitModal(discord.ui.Modal, title="你的性癖是?"):
    word_input = discord.ui.TextInput(
        label="請輸入一個性癖",
        min_length=1,
        max_length=20,
    )

    def __init__(self, game: GameSession):
        super().__init__()
        self.game = game

    async def on_submit(self, interaction: discord.Interaction):
        word = self.word_input.value.strip()
        self.game.words_pool[interaction.user] = word
        await interaction.response.send_message(
            f"你的性癖：**{word}**", ephemeral=True
        )

        if (
            len(self.game.words_pool) == len(self.game.players)
            and self.game.state == "COLLECTING"
        ):
            self.game.state = "CHECKING"  # 鎖定狀態
            await self.game.check_and_start(interaction)


# --- 重複時專門讓玩家透過私訊重新輸入的 Modal ---
class ReSubmitModal(discord.ui.Modal, title="你的性癖重複了"):
    word_input = discord.ui.TextInput(
        label="請重新輸入一個性癖",
        min_length=1,
        max_length=20,
    )

    def __init__(self, game: GameSession):
        super().__init__()
        self.game = game

    async def on_submit(self, interaction: discord.Interaction):
        word = self.word_input.value.strip()
        self.game.words_pool[interaction.user] = word
        await interaction.response.send_message(
            f"你的性癖：**{word}**", ephemeral=True
        )

        if (
            len(self.game.words_pool) == len(self.game.players)
            and self.game.state == "CHECKING"
        ):
            await self.game.check_and_start(interaction)


# --- 介面：投票狼人 ---
class VoteView(discord.ui.View):

    def __init__(self, game: GameSession, alive_players):
        super().__init__(timeout=60)
        self.game = game
        self.alive_players = alive_players
        self.votes = {}

        options = [
            discord.SelectOption(label=p.display_name, value=str(p.id))
            for p in alive_players
        ]
        self.select = discord.ui.Select(
            placeholder="狼人是誰?", options=options
        )
        self.select.callback = self.select_callback
        self.add_item(self.select)

    async def select_callback(self, interaction: discord.Interaction):
        if (
            interaction.user not in self.game.players
            or interaction.user in self.game.eliminated
        ):
            await interaction.response.send_message(
                "你已經出局或未參與遊戲！", ephemeral=True
            )
            return

        target_id = int(self.select.values[0])
        target = discord.utils.get(self.game.players, id=target_id)

        self.votes[interaction.user] = target
        await interaction.response.send_message(
            f"你投給：**{target.display_name}**", ephemeral=True
        )

        if len(self.votes) >= len(self.alive_players):
            self.stop()


# --- Discord Slash 指令 ---


@client.tree.command(name="start_game", description="開啟一局新的性癖狼人殺遊戲")
async def start_game(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)

    if interaction.channel.id in games:
        await interaction.followup.send(
            "本伺服器已經有一場正在進行的遊戲了！", ephemeral=True
        )
        return

    game = GameSession(interaction.channel, interaction.user)
    games[interaction.channel.id] = game

    class JoinView(discord.ui.View):

        def __init__(self):
            super().__init__(timeout=60)
            self.game = game

        @discord.ui.button(label="加入", style=discord.ButtonStyle.green)
        async def join_button(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ):
            if interaction.user in self.game.players:
                await interaction.response.send_message(
                    "已加入", ephemeral=True
                )
                return

            self.game.players.append(interaction.user)
            await interaction.response.send_message(
                f"{interaction.user.mention} 目前人數：{len(self.game.players)} ",
                ephemeral=False,
            )

    view = JoinView()
    await interaction.followup.send(
        "來玩性癖狼人殺吧!",
        view=view,
    )


@client.tree.command(name="begin", description="結束報名並開始遊戲（主持人專用）")
async def begin(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    game = games.get(interaction.channel.id)
    if not game:
        await interaction.followup.send(
            "本伺服器目前沒有進行中的遊戲", ephemeral=True
        )
        return
    if interaction.user != game.host:
        await interaction.followup.send(
            "只有開局者（主持人）才能開始遊戲！", ephemeral=True
        )
        return
    if len(game.players) < 2:  # DEBUG 測試設定為 2 人，正式建議調回 3
        await interaction.followup.send(
            "玩家人數不足 (至少需要 3 位玩家)", ephemeral=True
        )
        return

    game.state = "COLLECTING"
    game.words_pool.clear()

    await interaction.followup.send("點擊下方按鈕輸入你的性癖：")

    class SubmitButtonView(discord.ui.View):

        def __init__(self, game):
            super().__init__(timeout=120)
            self.game = game

        @discord.ui.button(
            label="點此輸入你的性癖",
            style=discord.ButtonStyle.primary,
        )
        async def submit_btn(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ):
            if interaction.user not in self.game.players:
                await interaction.response.send_message(
                    "你不是本局遊戲的玩家", ephemeral=True
                )
                return
            if interaction.user in self.game.words_pool:
                await interaction.response.send_message(
                    "已收到", ephemeral=True
                )
                return

            await interaction.response.send_modal(WordSubmitModal(self.game))

    await interaction.channel.send(view=SubmitButtonView(game))


@client.tree.command(name="cancel_game", description="強制結束目前的遊戲")
async def cancel_game(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    game = games.get(interaction.channel.id)
    if not game:
        await interaction.followup.send(
            "本伺服器沒有進行中的遊戲。", ephemeral=True
        )
        return
    if (
        interaction.user != game.host
        and not interaction.user.guild_permissions.administrator
    ):
        await interaction.followup.send(
            "只有主持人可以強制結束遊戲。", ephemeral=True
        )
        return

    game.end_game()
    await interaction.followup.send("遊戲已強制終止")


@client.tree.command(name="vote", description="結束討論並開始投票（主持人專用）")
async def vote_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    game = games.get(interaction.channel.id)
    if not game:
        await interaction.followup.send(
            "本頻道目前沒有進行中的遊戲", ephemeral=True
        )
        return

    if interaction.user != game.host:
        await interaction.followup.send(
            "只有主持人才能開啟投票", ephemeral=True
        )
        return

    if game.state != "DISCUSSING":
        await interaction.followup.send(
            "目前不是討論階段，無法投票", ephemeral=True
        )
        return

    # 切換為綠燈：讓卡在 await self.vote_event.wait() 的 game_loop 繼續執行
    game.vote_event.set()
    await interaction.followup.send("討論結束，開始投票", ephemeral=True)

# --- 啟動機器人 ---
if __name__ == "__main__":
    # 從環境變數讀取 Token，避免明碼寫在程式中
    TOKEN = os.getenv("DISCORD_TOKEN")
    
    if not TOKEN:
        print("錯誤：找不到 DISCORD_TOKEN，請檢查環境變數設定。")
    else:
        client.run(TOKEN)