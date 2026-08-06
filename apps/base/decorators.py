#decorators.py
from django.contrib.auth.decorators import user_passes_test

def solo_staff(user):
    return user.is_authenticated and user.is_staff

staff_required = user_passes_test(solo_staff, login_url='/login/')

# POR esto:
def en_grupo(nombre_grupo):
    """Devuelve un predicado que verifica si el usuario pertenece al grupo dado."""
    def check(user):
        return user.is_authenticated and (
            user.groups.filter(name=nombre_grupo).exists() or user.is_superuser
        )
    return check

almacen_required   = user_passes_test(en_grupo('ALMACEN'),   login_url='/login/')
calidad_required   = user_passes_test(en_grupo('CALIDAD'),   login_url='/login/')
vigilancia_required = user_passes_test(en_grupo('VIGILANCIA'), login_url='/login/')
compras_required   = user_passes_test(en_grupo('COMPRAS'),   login_url='/login/')
proveedor_required = user_passes_test(en_grupo('PROVEEDORES'), login_url='/login/')
# Grupo nuevo del rediseño de flujo Materia Prima (Fase 2, sesión 29):
# actor paralelo a ALMACEN para la recepción de OCs tipo Materia Prima.
materia_prima_required = user_passes_test(en_grupo('MATERIA_PRIMA'), login_url='/login/')

# Guard combinado para vistas accesibles por cualquier rol interno:
def es_staff_interno(user):
    return user.is_authenticated and (
        user.groups.filter(name__in=['ALMACEN','CALIDAD','VIGILANCIA','COMPRAS','MATERIA_PRIMA']).exists()
        or user.is_superuser
    )
staff_interno_required = user_passes_test(es_staff_interno, login_url='/login/')

def es_staff_o_proveedor(user):
    return user.is_authenticated and (
        user.groups.filter(name__in=['ALMACEN','CALIDAD','VIGILANCIA','COMPRAS','MATERIA_PRIMA','PROVEEDORES']).exists()
        or user.is_superuser
    )

staff_o_proveedor_required = user_passes_test(es_staff_o_proveedor, login_url='/login/')