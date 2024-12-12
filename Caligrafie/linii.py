from decimal import *
import sys
import math

FILE_PREFIX = """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<svg
   xmlns:dc="http://purl.org/dc/elements/1.1/"
   xmlns:cc="http://creativecommons.org/ns#"
   xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
   xmlns:svg="http://www.w3.org/2000/svg"
   xmlns="http://www.w3.org/2000/svg"
   xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"
   xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"
   sodipodi:docname="Copperplate-1.svg"
   inkscape:version="1.0beta1 (32d4812, 2019-09-19)"
   id="svg8"
   version="1.1"
   viewBox="0 0 210 297"
   height="297mm"
   width="210mm">
  <defs
     id="defs15067">
    <rect
       id="rect15812"
       height="10"
       width="158"
       y="5"
       x="10" />
  </defs>
  <sodipodi:namedview
     inkscape:window-maximized="0"
     inkscape:window-y="23"
     inkscape:window-x="38"
     inkscape:window-height="855"
     inkscape:window-width="1402"
     inkscape:snap-bbox="true"
     inkscape:snap-nodes="false"
     showgrid="false"
     inkscape:document-rotation="0"
     inkscape:current-layer="layer1"
     inkscape:document-units="mm"
     inkscape:cy="-63.460487"
     inkscape:cx="-63.891486"
     inkscape:zoom="1.4142136"
     inkscape:pageshadow="2"
     inkscape:pageopacity="0.0"
     borderopacity="1.0"
     bordercolor="#666666"
     pagecolor="#ffffff"
     id="base" />
  <metadata
     id="metadata5">
    <rdf:RDF>
      <cc:Work
         rdf:about="">
        <dc:format>image/svg+xml</dc:format>
        <dc:type
           rdf:resource="http://purl.org/dc/dcmitype/StillImage" />
        <dc:title></dc:title>
      </cc:Work>
    </rdf:RDF>
  </metadata>
  <g
     id="layer1"
     inkscape:groupmode="layer"
     inkscape:label="Strat 1">
"""

FILE_SUFFIX = """
  </g>
</svg>
"""

def writeFilePrefix(f):
    f.write(FILE_PREFIX)

def writeFileSuffix(f):
    f.write(FILE_SUFFIX)

#       style="fill:none;stroke:#A0A0A0;stroke-width:0.05px;stroke-linecap:butt;stroke-linejoin:miter;stroke-opacity:1" />
def lineText(x1, y1, x2, y2):
    return """
    <path
       inkscape:connector-curvature="0"
       id="path_%d_%d_%d_%d"
       d="M %d,%d %d,%d"
       style="fill:none;stroke:#A0A0A0;stroke-width:0.5px;stroke-linecap:butt;stroke-linejoin:miter;stroke-opacity:1" />
""" % (x1, y1, x2, y2, x1, y1, x2, y2)
# ;stroke-dasharray:0.1,0.1;stroke-dashoffset:0

def text(x1, y1, message):
    return """
    <text
       style="font-style:normal;font-weight:normal;font-size:5px;line-height:1.25;font-family:sans-serif;shape-inside:url(#rect15812);fill:#000000;fill-opacity:1;stroke:none"
       id="text15810"
       xml:space="preserve"><tspan
         x="%d"
         y="%d"
         sodipodi:role="line"><tspan>%s</tspan></tspan></text>
""" % (x1, y1, message)

def writeLine(f, x1, y1, x2, y2):
    f.write(lineText(x1, y1, x2, y2))

def writeText(f, x1, y1, message):
    f.write(text(x1, y1, message))

def generateRepeated(l):
    l1 = []
    while True:
        if l1 == []:
            l1 = l
        yield l1[0]
        l1 = l1[1:]

def writeLinesParallelX(f, x1, y1, x2, y2, dx):
    y = y1
    for d in generateRepeated(dx):
        y = y + d
        if y >= y2:
            break
        writeLine(f, x1, y, x2, y)

def writeLinesParallelY(f, x1, y1, x2, y2, dy):
    x = x1
    for d in generateRepeated(dy):
        x = x + d
        if x >= x2:
            return
        writeLine(f, x, y1, x, y2)

def writeLinesAngleX(f, x1, y1, x2, y2, u, du):
    x = x1
    urad = u * math.pi / 180
    s = math.sin(urad)
    t = math.tan(urad)
    for d in generateRepeated(du):
        ds = int(d / s)
        x = x + ds
        y = t * (x - x1) + y1
        # y -y1 = t * (x - x1)
        # x - x1 = (y - y1) / t
        # x = x1 + (y - y1) / t

        yprim = t * (x - x2) + y1
        xprim = (y - y2) / t + x1

        if y <= y2:
            yend = y
            xend = x1
        else:
            yend = y2
            xend = xprim
        if x <= x2:
            xstart = x
            ystart = y1
        else:
            xstart = x2
            ystart = yprim
        if xprim > x2:
            break

        writeLine(f, xstart, ystart, xend, yend)

def writeLinesAngleY(f, x1, y1, x2, y2, u, du):
    y = y1
    urad = u * math.pi / 180
    s = math.sin(urad)
    t = math.tan(urad)
    for d in generateRepeated(du):
        ds = int(d / s)
        y = y + ds
        x = t * (y - y1) + x1
        # x -x1 = t * (y - y1)
        # y - y1 = (x - x1) / t
        # y = y1 + (x - x1) / t

        xprim = t * (y - y2) + x1
        yprim = (x - x2) / t + y1

        if x <= x2:
            xend = x
            yend = y1
        else:
            xend = x2
            yend = yprim
        if y <= y2:
            ystart = y
            xstart = x1
        else:
            ystart = y2
            xstart = xprim
        if yprim > y2:
            break

        writeLine(f, x1 + x2 - xstart, ystart, x1 + x2 - xend, yend)

def writeFile (name, writer):
    with open(name + ".svg", "wt") as f:
        writeFilePrefix(f)

        writeLine(f, 10, 10, 200, 10)
        writeLine(f, 200, 10, 200, 287)
        writeLine(f, 200, 287, 10, 287)
        writeLine(f, 10, 287, 10, 10)

        writeText(f, 10, 10, name)

        writer(f)
        writeFileSuffix(f)

foundational_3mm = [6, 12, 6, 6]

def copperplateX(f, x1, y1, x2, y2, d):
    unghi = 55
    writeLinesParallelX(f, x1, y1, x2, y2, [d])
    writeLinesAngleX(f, 10, 10, 200, 287, unghi, [0.7 * d])

def copperplateUnequalX(f, x1, y1, x2, y2, d):
    unghi = 55
    writeLinesParallelX(f, x1, y1, x2, y2, [1.5*d, d, 1.5*d])
    writeLinesAngleX(f, 10, 10, 200, 287, unghi, [2 * d])

def copperplateY(f, x1, y1, x2, y2, d):
    unghi = 55
    writeLinesParallelY(f, x1, y1, x2, y2, [d])
    writeLinesAngleY(f, 10, 10, 200, 287, unghi, [0.7 * d])

def copperplateUnequalY(f, x1, y1, x2, y2, d):
    unghi = 55
    writeLinesParallelY(f, x1, y1, x2, y2, [1.5*d, d, 1.5*d])
    writeLinesAngleY(f, 10, 10, 200, 287, unghi, [2 * d])

def arhaicBicolorY(f, x1, y1, x2, y2, d):
    writeLinesParallelY(f, x1, y1, x2, y2, [10 * d, 2 * d])

def arhaicArnotaY(f, x1, y1, x2, y2, d):
    writeLinesParallelY(f, x1, y1, x2, y2, [2 * d, 8 * d, 2 * d])

def arhaicInaltY(f, x1, y1, x2, y2, d):
    writeLinesParallelY(f, x1, y1, x2, y2, [15 * d, 2 * d])

def foundationalY(f, x1, y1, x2, y2, d):
    writeLinesParallelY(f, x1, y1, x2, y2, [2 * d, 4 * d, 2 * d, 2 * d])

def gothic1Y(f, x1, y1, x2, y2, d):
    writeLinesParallelY(f, x1, y1, x2, y2, [2 * d, 5.5 * d, 2 * d, 2 * d])

def main():
    # A4, 1 cm around edges

    writeFile(
        "copperplate-unequal-portrait-5mm",
        lambda f : copperplateUnequalX(f, 10, 10, 200, 287, 5))
    writeFile(
        "copperplate-unequal-5mm",
        lambda f : copperplateUnequalY(f, 10, 10, 200, 287, 5))
    writeFile(
        "copperplate-equal-6mm",
        lambda f : copperplateY(f, 10, 10, 200, 287, 6))

    for nib_width in ["0.85", "1.1", "1.35", "1.5", "1.6", "2", "2.2", "2.4", "2.8", "3.2", "3.8", "6"]:
        width_float = float(nib_width)
        writeFile(
            "foundational-%smm" % nib_width,
            lambda f : foundationalY(f, 10, 10, 200, 287, width_float))
        writeFile(
            "gothic-1-%smm" % nib_width,
            lambda f : gothic1Y(f, 10, 10, 200, 287, width_float))
        writeFile(
            "arhaic-romanesc-bicolor-%smm" % nib_width,
            lambda f : arhaicBicolorY(f, 10, 10, 200, 287, width_float))
        writeFile(
            "arhaic-romanesc-arnota-%smm" % nib_width,
            lambda f : arhaicArnotaY(f, 10, 10, 200, 287, width_float))
        writeFile(
            "arhaic-romanesc-inalt-%smm" % nib_width,
            lambda f : arhaicInaltY(f, 10, 10, 200, 287, width_float))

if __name__ == "__main__":
    main()