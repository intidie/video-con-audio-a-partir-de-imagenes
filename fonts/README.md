# Fuentes para los subtítulos

La sección 5 de la app dibuja los subtítulos con la fuente **Georgia**. Copia aquí los archivos
(los puedes sacar de `C:\Windows\Fonts` en Windows o de macOS):

- `georgia.ttf` (regular, obligatorio)
- `georgiai.ttf` (itálica, opcional; si falta, libass genera una itálica sintética como Premiere)
- `georgiab.ttf` (negrita, opcional)

Georgia es una fuente con licencia de Microsoft: si tu repositorio es público, considera no
subirla a GitHub (agrega `fonts/*.ttf` a `.gitignore` y súbela solo al servidor), o usa
**Gelasio** (licencia OFL, métricas compatibles con Georgia) guardándola aquí como `Gelasio-Regular.ttf`.

Si no hay ninguna de las dos, la app usa DejaVu Serif y muestra un aviso.
