# Plan nodes: connectors, decision branches, the RAG node

The kinds of step are one table, `tadamoo/planner/nodes.py` `NODE_SPECS`; the checks of a plan, the tools of the Chat (`plan_propose_change`) and the Plans
page (palette, inspector, graph) all read it, so a field added there appears everywhere.

## Connectors (ports)

A port has an `id`, a `kind` and a label. `flow`: the step below waits for this one. `input`: data given to a step; nobody waits and nothing is ordered.
`PORT_IN` / `PORT_OUT` in `nodes.py` say which steps have which; `catalogue()` carries them to the page as `ports: {in, out}` (a decision's output is
`"branches"`).

| Step | Inputs | Outputs |
|---|---|---|
| any step | `in` (flow) | `out` (flow) |
| action | `in` (flow), `rag` (input) | `out` (flow) |
| decision | `in` (flow) | one `flow` port for each branch, named by the branch `id`, labelled with its label |
| rag | none | `rag` (input) |

An edge goes from an output port of one step to an input port of another of the same kind. In a diff an edge is `[parent, child]`, `[parent, child, port]` or
`{from: {node, port}, to: {node}}`; the port may be left out when the parent has only one (or, for a decision stored before branches, when a branch has the
child's name). A step's flow edges are in `dependencies` / `outputs` as before; a decision's and a RAG node's edges are also in `routes: {port: [child ids]}`
(an `input` edge only there, never in the dependencies, so it cannot be taken for "waits for"). `PlanDAG.edges()` lists every edge as
`{from: {node, port}, to: {node, port}, kind}`. Removing a step or a branch takes its edges with it.

## Decision

`spec`: `input` (an expression, the value the helper compares), `mode`, `on_no_match`, `branches`. A branch is `{id, label, condition}`; with the helper
`op` (`<  <=  =  !=  >=  >  between  in  contains  else`), `value` and `value2` the server builds the condition from the input (`build_condition`).
Labels are unique (at most 40 characters), `else` is at most one and last, a new branch gets an id and ids stay when branches are reordered.

- **`mode`**: `first` (default) takes the first branch that holds in the order shown, so overlapping ranges ("> 10", "10 to 30") do what the order says;
  `all` takes every branch that holds. The steps only the other branches lead to are skipped.
- **`else`** is taken when no other branch holds.
- **`on_no_match`** (when nothing holds and there is no else): `stop` (default) completes the decision with `state_variables.no_branch` (the conditions read, with
  values) and skips what is below it; the rest of the plan goes on and the page shows "No route". `ask` hands the decision to the person, as before
  (`state_variables.no_route`). A condition that cannot be read yet always stops the decision for the person.

A decision stored with a condition for each exit loads as `all`-mode branches named by their exits, with `on_no_match: ask` (`migrate_decision`, run when a
plan is loaded or a node is added); one with nothing to make branches from becomes a draft to set up.

## The RAG node and knowledge archives

A `rag` node (`spec.archives`: archive ids) is a source, resolved by the plan itself like a log line. Its `rag` port connects to actions only. The archives of an
action are those of the plan (`profile.rag_archives`, set with `plan_set_profile`), its own (`spec.rag_archives`) and those of each RAG node connected to it
(`tadamoo/planner/archives.py` `of_step`). When the agent runs `plan_step_start` the harness gives the step these archives (`ProjectRecall.bring`, through the
resolver with the function `plan_node`, so the cloud rule applies) and tells the model their names and what the route left out; `knowledge_search` then covers
them with the project's own. They are let go when the step is marked done, failed or skipped. A routine has `rag_archives` too: they are attached to the
conversation the run starts. The resolver is described in [rag-resolve.md](rag-resolve.md).

## The page

The graph draws a decision as a box that grows with its branches, each with its label beside its own connector (cut with an ellipsis, the whole text in the
tooltip and the accessible name). Connectors are 11 px dots; the touch area is a larger, invisible one. A RAG link is dashed and ends in a dot. Linking by drag
has a twin in the inspector: a Connections section with a select and a Connect button for each branch, or for a RAG node, and a button to disconnect each
one (44 px), which is how a phone or a keyboard connects. The branch editor is a list of rows (label, operator, value) with up, down and remove buttons.
Type sizes: title about 13 px, branch and port labels 11 px. The archive picker is `serve/web/ragpick.js`.

Tests: `tests/test_planner_ports.py`, `tests/test_planner_decisions.py`, `tests/test_planner_rag.py`, `serve/tests/test_plans_page.py`,
`serve/tests/plankinds_check.js`, `serve/tests/plankinds_dom_check.js`.
