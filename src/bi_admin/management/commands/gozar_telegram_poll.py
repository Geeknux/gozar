"""Answer Telegram bot commands by long polling (interactive mode only).

``gozar_worker`` does this in a thread (``POLL_IN_WORKER``); use this command
when the worker runs elsewhere or with ``POLL_IN_WORKER = False``. Only one
process may poll a bot at a time (Telegram answers 409 to a second one)."""

from django.core.management.base import BaseCommand, CommandError

from bi_admin.alerts import bot, telegram


class Command(BaseCommand):
    help = "Poll Telegram for /status, /alerts, /ack, /mute and /help from allowed users."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="one short poll, then exit")

    def handle(self, *args, once, **options):
        try:
            bot.require_interactive()
        except RuntimeError as exc:
            raise CommandError(f"Not polling: {exc}") from None
        self.stdout.write("Polling Telegram; Ctrl+C stops.")
        try:
            bot.run(once=once)
        except telegram.TelegramError as exc:
            raise CommandError(f"getUpdates failed: {exc}") from None
        except KeyboardInterrupt:
            pass
