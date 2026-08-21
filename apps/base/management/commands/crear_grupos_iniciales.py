from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand

GRUPOS_OPERATIVOS = ['COMPRAS', 'ALMACEN', 'VIGILANCIA', 'CALIDAD', 'MATERIA_PRIMA']


class Command(BaseCommand):
    help = (
        'Crea (si no existen) los 5 grupos operativos del Portal: '
        'COMPRAS, ALMACEN, VIGILANCIA, CALIDAD, MATERIA_PRIMA. '
        'Idempotente: correrlo varias veces no duplica ni falla.'
    )

    def handle(self, *args, **options):
        for nombre in GRUPOS_OPERATIVOS:
            grupo, creado = Group.objects.get_or_create(name=nombre)
            if creado:
                self.stdout.write(self.style.SUCCESS(f'Creado: {nombre}'))
            else:
                self.stdout.write(f'Ya existía: {nombre}')

        self.stdout.write(self.style.SUCCESS('Grupos operativos listos.'))
