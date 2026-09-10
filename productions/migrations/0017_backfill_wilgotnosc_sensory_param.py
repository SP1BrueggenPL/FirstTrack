from django.db import migrations


def backfill_wilgotnosc(apps, schema_editor):
    # Istniejące checklisty mają już utworzone wiersze parametrów (patrz
    # ChecklistAfter._create_default_params) dla starej listy PARAM_CHOICES -
    # bez tego nowy parametr "Wilgotność" pojawiłby się tylko w checklistach
    # utworzonych od teraz, a nie w tych już rozpoczętych/w toku.
    ChecklistAfter = apps.get_model('productions', 'ChecklistAfter')
    SensoryParam = apps.get_model('productions', 'SensoryParam')
    for checklist in ChecklistAfter.objects.exclude(sensory_params__param='wilgotnosc'):
        SensoryParam.objects.create(checklist=checklist, param='wilgotnosc')


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("productions", "0016_alter_sensoryparam_param"),
    ]

    operations = [
        migrations.RunPython(backfill_wilgotnosc, noop_reverse),
    ]
