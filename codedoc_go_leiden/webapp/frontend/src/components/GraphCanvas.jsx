import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from "react";
import cytoscape from "cytoscape";
import fcose from "cytoscape-fcose";
import { edgeWidth, nodeSize, stylesheet, tableInfo } from "../graphStyle";

cytoscape.use(fcose);

const BG = "#0f1117";
const MAX_FIT_ZOOM = 1.3; // a small graph is not blown up to fill the canvas

function clampZoom(cy) {
  if (cy.zoom() > MAX_FIT_ZOOM) {
    cy.animate({ zoom: MAX_FIT_ZOOM, center: { eles: cy.elements(":visible") }, duration: 200 });
  }
}

// A hub's neighbourhood (a function with 189 callers) is a star: a force
// layout packs its leaves into a ball. It gets rings instead: the focus in
// the middle, then its neighbours, then theirs, each ring holding more nodes
// than the one inside it, grouped by kind.
function ringLayout(cy, center) {
  const rank = new Map();
  const groups = [[], [], []];
  cy.nodes().forEach((n) => {
    if (n.id() !== center) groups[Math.min(Math.max(n.data("depth") ?? 1, 1), 2)].push(n);
  });
  let ring = 0;
  groups.slice(1).forEach((nodes, i) => {
    nodes.sort((a, b) => a.data("kind").localeCompare(b.data("kind")) || a.data("label").localeCompare(b.data("label")));
    for (let at = 0; at < nodes.length; ring += 1) {
      const capacity = 18 + 12 * ring;
      nodes.slice(at, at + capacity).forEach((n) => rank.set(n.id(), (2 - i) * 1000 - ring));
      at += capacity;
    }
  });
  return {
    name: "concentric",
    concentric: (n) => (n.id() === center ? 10000 : rank.get(n.id()) ?? 0),
    levelWidth: () => 1,
    minNodeSpacing: 14,
    avoidOverlap: true,
    startAngle: (3 / 2) * Math.PI,
    animate: false,
    fit: true,
    padding: 40,
  };
}

// fcose for every view but a large or star-shaped focus view (ringLayout).
// `fresh` lays everything out anew; otherwise only the nodes just added move,
// around the node that was expanded.
function layoutOptions(kind, cy, { fresh, count, center }) {
  if (fresh && kind === "focus" && center && (count > 120 || cy.getElementById(center).degree(false) > 50)) {
    return ringLayout(cy, center);
  }
  // The topic map: dense dependencies between few, large nodes; pull it together.
  const compact = kind === "topics";
  // Large views (an etcd package map) skip label measurement and animation.
  const large = count > 150 || cy.edges().length > 400;
  return {
    name: "fcose",
    quality: "default",
    randomize: fresh,
    animate: !large,
    animationDuration: fresh ? 600 : 400,
    fit: fresh,
    padding: 40,
    nodeDimensionsIncludeLabels: !large,
    idealEdgeLength: (edge) =>
      compact ? 70 : tableInfo(edge.data("table")).length || (tableInfo(edge.data("table")).dim ? 60 : 110),
    edgeElasticity: (edge) => (tableInfo(edge.data("table")).dim ? 0.6 : 0.4),
    nodeRepulsion: () => (compact ? 6000 : 14000),
    nodeSeparation: 80,
    gravity: compact ? 0.8 : 0.3,
    gravityCompound: compact ? 2 : 1.2,
    nestingFactor: 0.12,
    numIter: 2500,
    tile: true,
    tilingPaddingVertical: 20,
    tilingPaddingHorizontal: 20,
    packComponents: false,
  };
}

// Puts the 「未分類」 topics to the right of everything else (spec L-R42).
function placeApart(cy) {
  const apart = cy.nodes("[?unassigned]");
  if (apart.empty()) return;
  const bb = cy.nodes().not(apart).not(":parent").boundingBox();
  apart.forEach((n, i) => {
    n.position({ x: (Number.isFinite(bb.x2) ? bb.x2 : 0) + 180, y: (Number.isFinite(bb.y1) ? bb.y1 : 0) + 60 + i * 140 });
  });
}

function nodeData(n, center) {
  return { ...n, w: nodeSize(n, { center: n.id === center }) };
}

function edgeData(e) {
  return { ...e, ew: edgeWidth(e) };
}

// Hides what the toggles hide; in the focus view also every node that no
// longer connects to a root (the focus, expanded nodes, path ends) through
// visible edges. Only elements whose state changes are touched, so a large
// view is not restyled as a whole.
function applyVisibility(cy, { hiddenTables, hideTests, hideGenerated, roots, prune }) {
  const off = new Set();
  cy.edges().forEach((e) => {
    if (hiddenTables.has(e.data("table"))) off.add(e.id());
  });
  cy.nodes().forEach((n) => {
    const d = n.data();
    if (!roots.has(d.id) && ((hideTests && d.test) || (hideGenerated && d.generated))) off.add(d.id);
  });
  if (prune) {
    const seen = new Set();
    const stack = [];
    roots.forEach((id) => {
      const n = cy.getElementById(id);
      if (n.nonempty() && !off.has(id)) {
        seen.add(id);
        stack.push(n);
      }
    });
    while (stack.length) {
      const n = stack.pop();
      n.connectedEdges().forEach((e) => {
        if (off.has(e.id())) return;
        const other = e.source().same(n) ? e.target() : e.source();
        if (off.has(other.id()) || seen.has(other.id())) return;
        seen.add(other.id());
        stack.push(other);
      });
    }
    cy.nodes().forEach((n) => {
      if (!n.isParent() && !seen.has(n.id())) off.add(n.id());
    });
  }
  cy.nodes(":parent").forEach((p) => {
    if (p.children().every((c) => off.has(c.id()))) off.add(p.id());
  });
  cy.batch(() => {
    cy.elements().forEach((ele) => {
      if (ele.hasClass("off") !== off.has(ele.id())) ele.toggleClass("off", off.has(ele.id()));
    });
  });
}

// Adds what `graph` has and the canvas lacks (removing what it no longer has);
// on a fresh graph everything is replaced. Returns the nodes added.
function syncElements(cy, graph, fresh) {
  const added = [];
  cy.batch(() => {
    if (fresh) cy.elements().remove();
    else {
      const want = new Set([...graph.nodes.map((n) => n.id), ...graph.edges.map((e) => e.id)]);
      cy.elements().filter((ele) => !want.has(ele.id())).remove();
    }
    const anchor = graph.anchor ? cy.getElementById(graph.anchor) : null;
    const at = anchor && anchor.nonempty() ? anchor.position() : null;
    const ordered = [...graph.nodes].sort((a, b) => (a.parent ? 1 : 0) - (b.parent ? 1 : 0));
    const newNodes = ordered.filter((n) => cy.getElementById(n.id).empty());
    newNodes.forEach((n, i) => {
      const angle = (2 * Math.PI * i) / Math.max(newNodes.length, 1);
      const radius = 80 + 14 * Math.sqrt(newNodes.length);
      const position = at ? { x: at.x + radius * Math.cos(angle), y: at.y + radius * Math.sin(angle) } : undefined;
      added.push(cy.add({ group: "nodes", data: nodeData(n, graph.center), position }));
    });
    if (!fresh) {
      graph.nodes.forEach((n) => {
        const ele = cy.getElementById(n.id);
        if (ele.nonempty()) ele.data({ label: n.label, w: nodeSize(n, { center: n.id === graph.center }) });
      });
    }
    graph.edges.forEach((e) => {
      if (cy.getElementById(e.id).empty() && cy.getElementById(e.source).nonempty() && cy.getElementById(e.target).nonempty()) {
        cy.add({ group: "edges", data: edgeData(e) });
      }
    });
    cy.nodes(".center").removeClass("center");
    if (graph.center) cy.getElementById(graph.center).addClass("center");
  });
  return added;
}

function markSelected(cy, selectedId) {
  cy.batch(() => {
    cy.$(":selected").unselect();
    if (selectedId) cy.getElementById(selectedId).select();
  });
}

function markPath(cy, pathIds) {
  cy.batch(() => {
    cy.elements(".path").removeClass("path");
    (pathIds || []).forEach((id) => cy.getElementById(id).addClass("path"));
  });
}

const GraphCanvas = forwardRef(function GraphCanvas(
  { graph, layout, hiddenTables, hideTests, hideGenerated, prune, selectedId, pathIds, onSelect, onExpand, onHover },
  ref
) {
  const containerRef = useRef(null);
  const cyRef = useRef(null);
  const keyRef = useRef(null);
  const handlers = useRef({ onSelect, onExpand, onHover });
  const latest = useRef({});
  // The key of the last graph whose fresh layout finished: until then an
  // overlay covers the canvas (its dots animate while the layout runs).
  const [laidOut, setLaidOut] = useState(null);

  useEffect(() => {
    handlers.current = { onSelect, onExpand, onHover };
    latest.current = { hiddenTables, hideTests, hideGenerated, prune, selectedId, pathIds };
  });

  useEffect(() => {
    const cy = cytoscape({
      container: containerRef.current,
      style: stylesheet(),
      minZoom: 0.04,
      maxZoom: 4,
      boxSelectionEnabled: false,
      selectionType: "single",
    });
    cyRef.current = cy;
    keyRef.current = null;
    cy.on("tap", "node", (evt) => handlers.current.onSelect?.(evt.target.id()));
    cy.on("tap", (evt) => {
      if (evt.target === cy) handlers.current.onSelect?.(null);
    });
    cy.on("dbltap", "node", (evt) => handlers.current.onExpand?.(evt.target.id()));
    // Hover fades all but the node's neighbourhood; on a large view, where
    // restyling every element costs a visible pause, it marks the node only.
    cy.on("mouseover", "node", (evt) => {
      const node = evt.target;
      if (cy.elements().length > 300) node.addClass("hovered");
      else {
        const hood = node.closedNeighborhood().union(node.ancestors());
        cy.batch(() => {
          cy.elements().not(hood).addClass("faded");
          hood.addClass("focused");
        });
      }
      handlers.current.onHover?.(node.data());
    });
    cy.on("mouseout", "node", () => {
      cy.batch(() => cy.elements(".faded, .focused, .hovered").removeClass("faded focused hovered"));
      handlers.current.onHover?.(null);
    });
    return () => {
      cy.destroy();
      cyRef.current = null;
    };
  }, []);

  // Toggles, selection and path highlight, from the latest props.
  const decorate = (cy, g) => {
    const p = latest.current;
    applyVisibility(cy, { hiddenTables: p.hiddenTables, hideTests: p.hideTests, hideGenerated: p.hideGenerated,
                          roots: new Set(g.roots || []), prune: p.prune });
    markSelected(cy, p.selectedId);
    markPath(cy, p.pathIds);
  };

  // Sync the elements, then lay out what is new. A fresh graph waits one
  // frame, so the overlay is painted before the layout blocks the thread.
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy || !graph) return undefined;
    const fresh = keyRef.current !== graph.key;
    const lay = (added) => {
      if (!fresh && added.length === 0) return;
      if (cy.nodes().empty()) {
        setLaidOut(graph.key);
        return;
      }
      const options = layoutOptions(layout, cy, { fresh, count: cy.nodes().length, center: graph.center });
      if (!fresh) {
        const addedIds = new Set(added.map((n) => n.id()));
        options.fixedNodeConstraint = cy
          .nodes()
          .filter((n) => !addedIds.has(n.id()) && !n.isParent())
          .map((n) => ({ nodeId: n.id(), position: { ...n.position() } }));
      }
      const run = cy.layout(options);
      run.one("layoutstop", () => {
        if (layout === "topics") placeApart(cy);
        if (fresh && (layout === "topics" || !options.animate)) cy.fit(undefined, 40);
        if (fresh) {
          clampZoom(cy);
          setLaidOut(graph.key);
        } else {
          cy.animate({ fit: { eles: cy.elements(":visible"), padding: 40 }, duration: 300, complete: () => clampZoom(cy) });
        }
      });
      try {
        run.run();
      } catch (e) {
        setLaidOut(graph.key); // never leave the overlay up
        throw e;
      }
    };
    if (!fresh) {
      const added = syncElements(cy, graph, false);
      decorate(cy, graph);
      lay(added);
      return undefined;
    }
    const timer = setTimeout(() => {
      cy.resize(); // the details panel may have just narrowed the canvas; cytoscape notices 100 ms later
      keyRef.current = graph.key;
      const added = syncElements(cy, graph, true);
      decorate(cy, graph);
      lay(added);
    }, 30);
    return () => clearTimeout(timer);
  }, [graph, layout]);

  // Once the canvas shows `graph` (keyRef), the toggles apply right away.
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy || !graph || keyRef.current !== graph.key) return;
    applyVisibility(cy, { hiddenTables, hideTests, hideGenerated, roots: new Set(graph.roots || []), prune });
  }, [graph, hiddenTables, hideTests, hideGenerated, prune]);

  useEffect(() => {
    const cy = cyRef.current;
    if (cy && graph && keyRef.current === graph.key) markSelected(cy, selectedId);
  }, [selectedId, graph]);

  useEffect(() => {
    const cy = cyRef.current;
    if (cy && graph && keyRef.current === graph.key) markPath(cy, pathIds);
  }, [pathIds, graph]);

  useImperativeHandle(ref, () => ({
    fit() {
      const cy = cyRef.current;
      cy?.resize();
      cy?.animate({ fit: { eles: cy.elements(":visible"), padding: 40 }, duration: 250, complete: () => clampZoom(cy) });
    },
    zoom(factor) {
      const cy = cyRef.current;
      if (!cy) return;
      const level = Math.min(Math.max(cy.zoom() * factor, cy.minZoom()), cy.maxZoom());
      cy.animate({ zoom: { level, renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } }, duration: 150 });
    },
    relayout() {
      const cy = cyRef.current;
      if (!cy) return;
      cy.resize();
      const options = layoutOptions(layout, cy, { fresh: true, count: cy.nodes(":visible").length, center: graph?.center });
      const run = cy.elements(":visible").layout(options);
      run.one("layoutstop", () => {
        if (layout === "topics") placeApart(cy);
        cy.fit(cy.elements(":visible"), 40);
        clampZoom(cy);
      });
      run.run();
    },
    png() {
      const cy = cyRef.current;
      return cy ? cy.png({ output: "blob", bg: BG, full: true, scale: 2, maxWidth: 6000, maxHeight: 6000 }) : null;
    },
  }));

  return (
    <>
      <div className="graph-canvas" ref={containerRef} />
      {graph && graph.key !== laidOut && (
        <div className="graph-overlay">
          <div className="loading-dots">
            <span />
            <span />
            <span />
          </div>
        </div>
      )}
    </>
  );
});

export default GraphCanvas;
