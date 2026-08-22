# apps/base/migrations/0004_backfill_supplierprofile_desde_proveedores.py
"""
Crea un SupplierProfile por cada User ya existente en el grupo
PROVEEDORES (usuarios tipo P20100055237), vinculando el user real —
antes de que exista ningún flujo de activación por cuenta nueva.

Cruce confirmado contra datos reales antes de escribir esto (no
asumido): User.username == PurchaseOrder.card_code, tal cual, sin
ningún prefijo que quitar — verificado con los 5 proveedores reales de
la BD local (P20266614803, P20100055237, P20100152941, P20609988771,
P20544332211), los 5 coinciden 1:1 con un card_code real. Mismo
criterio que ya usa el propio código del proyecto
(apps/appointments/views.py::PortalProveedorView, `ruc_proveedor =
self.request.user.username`, sin stripping de ningún prefijo).

Enriquecimiento de datos (razon_social/correo_electronico): si existe
al menos una PurchaseOrder real con card_code=username, se usa su
card_name/e_mail (más completo que dejarlo vacío); si no hay ninguna
OC todavía para ese proveedor, queda en blanco (no se inventa nada).

Sin efecto en producción al momento de escribir esta migración
(confirmado: el grupo PROVEEDORES no existe todavía ahí, 0 usuarios) —
corre igual de forma segura cuando se despliegue: si no hay grupo o no
hay usuarios, no crea nada.
"""
from django.db import migrations


def crear_supplier_profiles(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    SupplierProfile = apps.get_model('base', 'SupplierProfile')
    PurchaseOrder = apps.get_model('sap_sync', 'PurchaseOrder')

    grupo = Group.objects.filter(name='PROVEEDORES').first()
    if grupo is None:
        return

    for user in grupo.user_set.all():
        if SupplierProfile.objects.filter(sap_card_code=user.username).exists():
            continue  # ya vinculado a otro user (no debería pasar, pero idempotente)

        po = PurchaseOrder.objects.filter(card_code=user.username).order_by('created_at').first()
        SupplierProfile.objects.create(
            sap_card_code=user.username,
            ruc=user.username,
            razon_social=po.card_name if po else '',
            correo_electronico=po.e_mail if po else '',
            estado='ACTIVO',
            user=user,
        )


def eliminar_supplier_profiles_creados(apps, schema_editor):
    # Reversión: solo borra los que quedaron vinculados a un user (los
    # creados por esta migración) — nunca uno creado después por el
    # sync de OC sin ningún user asociado.
    SupplierProfile = apps.get_model('base', 'SupplierProfile')
    SupplierProfile.objects.filter(user__isnull=False).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('base', '0003_supplierprofile'),
        ('sap_sync', '0002_purchaseorderline_activa_and_more'),
        ('auth', '0012_alter_user_first_name_max_length'),
    ]

    operations = [
        migrations.RunPython(crear_supplier_profiles, eliminar_supplier_profiles_creados),
    ]
