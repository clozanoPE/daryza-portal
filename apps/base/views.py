# apps/base/views.py
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import SetPasswordForm
from django.shortcuts import redirect, render

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

    elif user.groups.filter(name='MATERIA_PRIMA').exists():
        return redirect('operations:panel_materia_prima')

    # Si es un administrador o no tiene grupo definido
    return redirect('/admin/')


@login_required
def cambiar_password_obligatorio(request):
    """
    Etapa 2.4 (Proveedores, sesión 89) — vista destino del
    ForzarCambioPasswordMiddleware. Un usuario sin SupplierProfile, o
    con debe_cambiar_password ya en False, no tiene nada que hacer acá
    (puede llegar por URL directa sin haber sido interceptado) — se
    manda de vuelta al ruteo normal en vez de mostrarle un formulario
    sin sentido.
    """
    perfil = getattr(request.user, 'supplier_profile', None)
    if perfil is None or not perfil.debe_cambiar_password:
        return redirect('home_router')

    if request.method == 'POST':
        form = SetPasswordForm(user=request.user, data=request.POST)
        if form.is_valid():
            form.save()
            # Sin esto, la sesión actual queda invalidada en la
            # siguiente request (Django detecta que el hash de password
            # guardado en la sesión ya no coincide con el real) — el
            # usuario perdería la sesión justo después de cambiar su
            # propia contraseña, contradiciendo el criterio confirmado
            # de "sin necesitar relogin".
            update_session_auth_hash(request, form.user)

            perfil.debe_cambiar_password = False
            perfil.save(update_fields=['debe_cambiar_password', 'updated_at'])

            return redirect('home_router')
    else:
        form = SetPasswordForm(user=request.user)

    return render(request, 'cambiar_password_obligatorio.html', {'form': form})