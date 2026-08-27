# apps/base/views.py
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import SetPasswordForm
from django.shortcuts import redirect, render

from . import services_recuperacion

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


def solicitar_recuperacion(request):
    """
    Etapa 2.5 (Proveedores, sesión 90) — sin @login_required a
    propósito: es exactamente el flujo para alguien que NO puede
    iniciar sesión. Punto 6 confirmado: la respuesta es SIEMPRE la
    misma, sin importar si el `username` ingresado existe o no — nunca
    se distingue el caso "usuario real, correo enviado" de "usuario
    inventado, no se hizo nada" en lo que ve la persona.
    """
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        if username:
            services_recuperacion.solicitar_recuperacion(username)
        return render(request, 'solicitar_recuperacion.html', {'enviado': True})

    return render(request, 'solicitar_recuperacion.html', {'enviado': False})


def confirmar_recuperacion(request, uidb64, token):
    """
    Etapa 2.5 (Proveedores, sesión 90) — sin @login_required, mismo
    motivo que solicitar_recuperacion. `resolver_usuario_desde_token`
    ya cubre uid mal formado / usuario inexistente-inactivo / token
    inválido-vencido-reutilizado con el mismo resultado (None) — el
    mensaje de rechazo es genérico a propósito, sin distinguir el
    motivo exacto (punto 7, "sin revelar más información de la
    necesaria").
    """
    user = services_recuperacion.resolver_usuario_desde_token(uidb64, token)
    if user is None:
        return render(request, 'confirmar_recuperacion.html', {'token_invalido': True})

    if request.method == 'POST':
        form = SetPasswordForm(user=user, data=request.POST)
        if form.is_valid():
            form.save()
            # Punto 5 confirmado: en el camino normal de este flujo NO
            # hay ninguna sesión autenticada como `user` en este
            # request (llega sin login) — update_session_auth_hash es
            # seguro de llamar igual: Django la implementa comparando
            # request.user == user primero, y si no coinciden (el caso
            # normal acá: request.user es AnonymousUser) solo hace
            # session.cycle_key() y no toca nada más. Ese cycle_key()
            # ya es un beneficio real de seguridad (rota el ID de
            # sesión en una acción sensible), y si el proveedor
            # resultara tener una sesión activa como este mismo user en
            # otra pestaña del mismo navegador (caso de borde posible,
            # no el esperado), la preserva correctamente en vez de
            # cerrarla. No hay ningún escenario en el que llamarla acá
            # sea incorrecto o arriesgado.
            update_session_auth_hash(request, form.user)

            perfil = getattr(user, 'supplier_profile', None)
            if perfil is not None and perfil.debe_cambiar_password:
                perfil.debe_cambiar_password = False
                perfil.save(update_fields=['debe_cambiar_password', 'updated_at'])

            return redirect('login')
    else:
        form = SetPasswordForm(user=user)

    return render(request, 'confirmar_recuperacion.html', {'form': form, 'token_invalido': False})