from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand

GRUPOS_OPERATIVOS = ['COMPRAS', 'ALMACEN', 'VIGILANCIA', 'CALIDAD', 'MATERIA_PRIMA']

# Etapa 2 (Proveedores, sesión 86): PROVEEDORES ya existe hoy en las BD
# reales (creado a mano hace mucho, sin migración ni fixture — mismo
# criterio que los 5 grupos operativos, ver sesión 29) pero nunca estuvo
# en este comando. El alta automática de proveedores (apps/base/
# supplier_onboarding.py) necesita garantizarlo en CUALQUIER entorno
# nuevo (una BD de pruebas recién creada, por ejemplo) sin depender de
# que alguien lo haya creado a mano antes — separado de GRUPOS_OPERATIVOS
# a propósito: es un rol externo (proveedor), no un grupo interno de staff.
GRUPOS_EXTERNOS = ['PROVEEDORES']


class Command(BaseCommand):
    help = (
        'Crea (si no existen) los 5 grupos operativos del Portal '
        '(COMPRAS, ALMACEN, VIGILANCIA, CALIDAD, MATERIA_PRIMA) y el '
        'grupo externo PROVEEDORES. Idempotente: correrlo varias veces '
        'no duplica ni falla.'
    )

    def handle(self, *args, **options):
        for nombre in GRUPOS_OPERATIVOS + GRUPOS_EXTERNOS:
            grupo, creado = Group.objects.get_or_create(name=nombre)
            if creado:
                self.stdout.write(self.style.SUCCESS(f'Creado: {nombre}'))
            else:
                self.stdout.write(f'Ya existía: {nombre}')

        self.stdout.write(self.style.SUCCESS('Grupos operativos y externos listos.'))
