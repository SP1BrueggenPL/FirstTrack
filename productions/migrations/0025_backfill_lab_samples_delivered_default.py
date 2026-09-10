from django.db import migrations


def backfill_default(apps, schema_editor):
    # Nowy domyślny wybór to "Nie" (patrz 0024) - checklisty już istniejące
    # (utworzone przed dodaniem tego pola/zmianą domyślnej wartości) mają
    # pustą wartość, którą trzeba dociągnąć do tego samego domyślnego stanu.
    ChecklistAfter = apps.get_model('productions', 'ChecklistAfter')
    ChecklistAfter.objects.filter(lab_samples_delivered='').update(lab_samples_delivered='nie')


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("productions", "0024_alter_checklistafter_lab_samples_delivered"),
    ]

    operations = [
        migrations.RunPython(backfill_default, noop_reverse),
    ]
