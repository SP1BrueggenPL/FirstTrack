from django import template

register = template.Library()


@register.filter
def attr(obj, name):
    """Dynamiczny getattr(obj, name) - do użycia gdy nazwa pola/atrybutu jest
    zmienną (np. z pętli), nie literałem, którego wymaga zwykłe {{ obj.name }}."""
    return getattr(obj, name, None)
