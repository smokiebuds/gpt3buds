import asyncio
import os
import traceback
from datetime import datetime, timedelta
from pathlib import Path

import discord

from models.openai_model import Model
from models.usage_service_model import UsageService

usage_service = UsageService(Path(os.environ.get("DATA_DIR", os.getcwd())))
model = Model(usage_service)


class ModerationResult:
    WARN = "warn"
    DELETE = "delete"
    NONE = "none"


class ThresholdSet:
    def __init__(self, h_t, hv_t, sh_t, s_t, sm_t, v_t, vg_t):
        self.keys = [
            "hate",
            "hate/threatening",
            "self-harm",
            "sexual",
            "sexual/minors",
            "violence",
            "violence/graphic",
        ]
        self.thresholds = [
            h_t,
            hv_t,
            sh_t,
            s_t,
            sm_t,
            v_t,
            vg_t,
        ]

    def moderate(self, text, response_message):
        category_scores = response_message["results"][0]["category_scores"]
        flagged = response_message["results"][0]["flagged"]

        for category, threshold in zip(self.keys, self.thresholds):
            if category_scores[category] > threshold:
                return (True, flagged)
        return (False, flagged)


class Moderation:
    def __init__(self, message, timestamp):
        self.message = message
        self.timestamp = timestamp

    @staticmethod
    def build_moderation_embed():
        # Create a discord embed to send to the user when their message gets moderated
        embed = discord.Embed(
            title="Your message was moderated",
            description="Our automatic moderation systems detected that your message was inappropriate and has been deleted. Please review the rules.",
            colour=discord.Colour.red(),
        )
        # Set the embed thumbnail
        embed.set_thumbnail(url="https://i.imgur.com/2oL8JSp.png")
        embed.set_footer(
            text="If you think this was a mistake, please contact the server admins."
        )
        return embed

    @staticmethod
    def build_admin_warning_message(
        moderated_message, deleted_message=None, timed_out=None
    ):
        embed = discord.Embed(
            title="Potentially unwanted message in the "
            + moderated_message.guild.name
            + " server",
            description=f"**Message from {moderated_message.author.mention}:** {moderated_message.content}",
            colour=discord.Colour.yellow(),
        )
        link = f"https://discord.com/channels/{moderated_message.guild.id}/{moderated_message.channel.id}/{moderated_message.id}"
        embed.add_field(name="Message link", value=link, inline=False)
        if deleted_message:
            embed.add_field(
                name="Message deleted by: ", value=deleted_message, inline=False
            )
        if timed_out:
            embed.add_field(name="User timed out by: ", value=timed_out, inline=False)
        return embed

    @staticmethod
    def build_admin_moderated_message(moderated_message, response_message):

        # Create a discord embed to send to the user when their message gets moderated
        embed = discord.Embed(
            title="A message was moderated in the "
            + moderated_message.message.guild.name
            + " server",
            description=f"Message from {moderated_message.message.author.mention} was moderated: {moderated_message.message.content}",
            colour=discord.Colour.yellow(),
        )
        # Get the link to the moderated message
        link = f"https://discord.com/channels/{response_message.guild.id}/{response_message.channel.id}/{response_message.id}"
        # set the link of the embed
        embed.add_field(name="Moderated message link", value=link, inline=False)
        return embed

    @staticmethod
    def determine_moderation_result(text, response):
        warn_set = ThresholdSet(0.005, 0.05, 0.05, 0.91, 0.1, 0.08, 0.1)
        delete_set = ThresholdSet(0.1, 0.1, 0.1, 0.95, 0.2, 0.6, 0.4)

        warn_result, flagged_warn = warn_set.moderate(text, response)
        delete_result, flagged_delete = delete_set.moderate(text, response)

        if delete_result:
            return ModerationResult.DELETE
        elif warn_result:
            return ModerationResult.WARN
        else:
            return ModerationResult.NONE

    # This function will be called by the bot to process the message queue
    @staticmethod
    async def process_moderation_queue(
        moderation_queue, PROCESS_WAIT_TIME, EMPTY_WAIT_TIME, moderations_alert_channel
    ):
        while True:
            try:
                # If the queue is empty, sleep for a short time before checking again
                if moderation_queue.empty():
                    await asyncio.sleep(EMPTY_WAIT_TIME)
                    continue

                # Get the next message from the queue
                to_moderate = await moderation_queue.get()

                # Check if the current timestamp is greater than the deletion timestamp
                if datetime.now().timestamp() > to_moderate.timestamp:
                    response = await model.send_moderations_request(
                        to_moderate.message.content
                    )
                    moderation_result = Moderation.determine_moderation_result(
                        to_moderate.message.content, response
                    )

                    if moderation_result == ModerationResult.DELETE:
                        # Take care of the flagged message
                        response_message = await to_moderate.message.reply(
                            embed=Moderation.build_moderation_embed()
                        )
                        # Do the same response as above but use an ephemeral message
                        await to_moderate.message.delete()

                        # Send to the moderation alert channel
                        if moderations_alert_channel:
                            await moderations_alert_channel.send(
                                embed=Moderation.build_admin_moderated_message(
                                    to_moderate, response_message
                                )
                            )
                    elif moderation_result == ModerationResult.WARN:
                        response_message = await moderations_alert_channel.send(
                            embed=Moderation.build_admin_warning_message(
                                to_moderate.message
                            ),
                        )
                        await response_message.edit(
                            view=ModerationAdminView(
                                to_moderate.message, response_message
                            )
                        )

                else:
                    await moderation_queue.put(to_moderate)
                # Sleep for a short time before processing the next message
                # This will prevent the bot from spamming messages too quickly
                await asyncio.sleep(PROCESS_WAIT_TIME)
            except:
                traceback.print_exc()
                pass


class ModerationAdminView(discord.ui.View):
    def __init__(self, message, moderation_message, nodelete=False):
        super().__init__(timeout=None)  # 1 hour interval to redo.
        self.message = message
        self.moderation_message = (moderation_message,)
        if not nodelete:
            self.add_item(DeleteMessageButton(self.message, self.moderation_message))
        self.add_item(
            TimeoutUserButton(self.message, self.moderation_message, 1, nodelete)
        )
        self.add_item(
            TimeoutUserButton(self.message, self.moderation_message, 6, nodelete)
        )
        self.add_item(
            TimeoutUserButton(self.message, self.moderation_message, 12, nodelete)
        )
        self.add_item(
            TimeoutUserButton(self.message, self.moderation_message, 24, nodelete)
        )


class DeleteMessageButton(discord.ui.Button["ModerationAdminView"]):
    def __init__(self, message, moderation_message):
        super().__init__(style=discord.ButtonStyle.danger, label="Delete Message")
        self.message = message
        self.moderation_message = moderation_message

    async def callback(self, interaction: discord.Interaction):

        # Get the user
        await self.message.delete()
        await interaction.response.send_message(
            "This message was deleted", ephemeral=True, delete_after=10
        )
        await self.moderation_message[0].edit(
            embed=Moderation.build_admin_warning_message(
                self.message, deleted_message=interaction.user.mention
            ),
            view=ModerationAdminView(
                self.message, self.moderation_message, nodelete=True
            ),
        )


class TimeoutUserButton(discord.ui.Button["ModerationAdminView"]):
    def __init__(self, message, moderation_message, hours, nodelete):
        super().__init__(style=discord.ButtonStyle.danger, label=f"Timeout {hours}h")
        self.message = message
        self.moderation_message = moderation_message
        self.hours = hours
        self.nodelete = nodelete

    async def callback(self, interaction: discord.Interaction):
        # Get the user id
        try:
            await self.message.delete()
        except:
            pass

        try:
            await self.message.author.timeout(
                until=discord.utils.utcnow() + timedelta(hours=self.hours),
                reason="Breaking the server chat rules",
            )
        except Exception as e:
            traceback.print_exc()
            pass

        await interaction.response.send_message(
            f"This user was timed out for {self.hours} hour(s)",
            ephemeral=True,
            delete_after=10,
        )
        moderation_message = (
            self.moderation_message[0][0]
            if self.nodelete
            else self.moderation_message[0]
        )
        await moderation_message.edit(
            embed=Moderation.build_admin_warning_message(
                self.message,
                deleted_message=interaction.user.mention,
                timed_out=interaction.user.mention,
            ),
            view=ModerationAdminView(
                self.message, self.moderation_message, nodelete=True
            ),
        )
