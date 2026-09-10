from django.core.management.base import BaseCommand

from productions.views import (
    _send_24h_before_production_reminders,
    _send_due_production_reminders,
)


class Command(BaseCommand):
    help = (
        'Wysyła przypomnienia o produkcjach zaplanowanych na dziś oraz za 24h '
        '(stała pula adresów + zespół danej produkcji). Bezpieczne do '
        'wielokrotnego uruchamiania tego samego dnia - każda produkcja dostaje '
        'każde przypomnienie tylko raz. Przydatne do podpięcia pod harmonogram '
        '(np. Azure WebJob), niezależnie od automatycznego wysyłania przy '
        'wejściu na dashboard.'
    )

    def handle(self, *args, **options):
        _send_due_production_reminders()
        _send_24h_before_production_reminders()
        self.stdout.write(self.style.SUCCESS(
            'Sprawdzono produkcje zaplanowane na dziś i za 24h, wysłano brakujące przypomnienia.'
        ))
