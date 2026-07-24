from django.core.management.base import BaseCommand

from productions.views import _send_due_production_reminders


class Command(BaseCommand):
    help = (
        'Wysyła przypomnienia o produkcjach zaplanowanych na dziś (stała pula '
        'adresów + zespół danej produkcji). Bezpieczne do wielokrotnego '
        'uruchamiania tego samego dnia - każda produkcja dostaje przypomnienie '
        'tylko raz. Przydatne do podpięcia pod harmonogram (np. Azure WebJob), '
        'niezależnie od automatycznego wysyłania przy wejściu na dashboard.'
    )

    def handle(self, *args, **options):
        _send_due_production_reminders()
        self.stdout.write(self.style.SUCCESS(
            'Sprawdzono produkcje zaplanowane na dziś i wysłano brakujące przypomnienia.'
        ))
