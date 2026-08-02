# apps/base/views.py
from django.shortcuts import redirect
from django.contrib.auth.decorators import login_required

@login_required
def redirect_by_role(request):
    """
    Detecta el grupo del usuario y lo redirige a su panel específico.
    """
    user = request.user

    if user.groups.filter(name='PROVEEDORES').exists():
        return redirect('appointments:portal_proveedor')
    
    elif user.groups.filter(name='ALMACEN').exists():
        return redirect('operations:panel_almacen')
    
    elif user.groups.filter(name='CALIDAD').exists():
        return redirect('operations:panel_calidad')

    elif user.groups.filter(name='COMPRAS').exists():
        return redirect('operations:panel_compras')
    
    elif user.groups.filter(name='VIGILANCIA').exists():
        return redirect('operations:panel_vigilancia')

    # Si es un administrador o no tiene grupo definido
    return redirect('/admin/')