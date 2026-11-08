"""Call-bridge: orchestratore dell'agente telefonico AI sull'host Windows."""

__version__ = "0.1.0"

# PRIMA di qualunque import di torch (anche indiretto): fa restituire subito al
# sistema la memoria che torch libera. Senza, un servizio a riposo teneva
# committate decine di GiB gia' scaricati. Vedi callbridge/memoria.py.
from .memoria import configura_allocatore as _configura_allocatore

_configura_allocatore()
