"""Print the Telegram bot's username (``getMe``): checks the token works."""

from django.core.management.base import BaseCommand, CommandError

from bi_admin.alerts import telegram


class Command(BaseCommand):
    help = "Print the username of the bot the configured token belongs to (getMe)."

    def handle(self, *args, **options):
        cfg = telegram.config()
        if not cfg.token_configured:
            raise CommandError(
                f"No bot token: set the environment variable {cfg.status()['token_env']}."
            )
        try:
            me = telegram.Client(cfg).get_me()
        except telegram.TelegramError as exc:
            raise CommandError(f"getMe failed: {exc}") from None
        self.stdout.write(f"@{me.get('username', '?')} ({me.get('first_name', '')})")
        state = "enabled" if cfg.enabled else "not enabled"
        self.stdout.write(
            f"Alert channel {state}; {len(cfg.allowed)} allowed chat id(s); "
            f"interactive mode {'on' if cfg.interactive else 'off'}."
        )
        for problem in cfg.problems:
            self.stderr.write(self.style.WARNING(problem["message"]))
