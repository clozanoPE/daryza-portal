# apps/scheduling/migrations/0001_initial.py
#
# Migración inicial de la app scheduling.
# Crea las tablas: scheduling_scheduletemplate y scheduling_weeklyslotRule.
#
# DEPENDENCIAS: No depende de otras migraciones (tablas independientes).
# CAPAS AFECTADAS: Base de datos PostgreSQL.

from django.db import migrations, models
import django.db.models.deletion
import django.core.validators


class Migration(migrations.Migration):

    initial = True

    dependencies = [
    ]

    operations = [
        migrations.CreateModel(
            name='ScheduleTemplate',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('nombre', models.CharField(
                    max_length=100,
                    help_text="Nombre descriptivo de la plantilla, ej: 'Semana Estándar Lurin'"
                )),
                ('descripcion', models.TextField(blank=True, null=True,
                                                  help_text='Notas internas del personal de Almacén.')),
                ('activa', models.BooleanField(
                    default=True,
                    help_text='Solo la plantilla activa se ofrece como opción de base para nuevas semanas.'
                )),
            ],
            options={
                'verbose_name': 'Plantilla de Horario',
                'verbose_name_plural': 'Plantillas de Horario',
                'ordering': ['-activa', 'nombre'],
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='WeeklySlotRule',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name='ID')),
                ('dia_semana', models.IntegerField(
                    choices=[
                        (0, 'Lunes'), (1, 'Martes'), (2, 'Miércoles'),
                        (3, 'Jueves'), (4, 'Viernes'), (5, 'Sábado'),
                    ],
                    help_text='Día de la semana (0=Lunes, 5=Sábado).'
                )),
                ('hora', models.TimeField(
                    help_text='Hora de inicio del slot, ej: 09:00'
                )),
                ('capacidad', models.PositiveIntegerField(
                    default=1,
                    validators=[django.core.validators.MinValueValidator(1)],
                    help_text='Máximo de citas permitidas en este horario. Por defecto 1.'
                )),
                ('activa', models.BooleanField(
                    default=True,
                    help_text='Si está desactivada, no genera AppointmentSlots al aplicar la plantilla.'
                )),
                ('template', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='reglas',
                    to='scheduling.scheduletemplate',
                    help_text='Plantilla a la que pertenece esta regla.'
                )),
            ],
            options={
                'verbose_name': 'Regla de Horario Semanal',
                'verbose_name_plural': 'Reglas de Horario Semanal',
                'ordering': ['dia_semana', 'hora'],
            },
        ),
        migrations.AddConstraint(
            model_name='weeklyslotRule',
            constraint=models.UniqueConstraint(
                fields=['template', 'dia_semana', 'hora'],
                name='unique_regla_por_plantilla_dia_hora'
            ),
        ),
    ]
