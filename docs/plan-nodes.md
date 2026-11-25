# Plan nodes: connectors, decision branches, the RAG node

Where a plan begins (start nodes, schedules in words, a step that starts another plan) is in [plan-start.md](plan-start.md); the request to the person, taking a run up again and recovering a failed step in [plan-runs.md](plan-runs.md).

The kinds of step are one table, `tadamoo/planner/nodes.py` `NODE_SPECS`; the checks of a plan, the tools of the Chat (`plan_propose_change`) and the Plans
page (palette, inspector, graph) all read it, so a field added there appears everywhere.

**Rule: what a plan starts with is a parameter, not a question.** A value the person gives before a run (the edition, the folder, the choices of a production) is a `parameter` node with a default,
set in the Parameters tab; an `input` node is only for what a run needs from the person while it goes on (an approval, a task, a value a later step asks for). A plan must be able to start with
nothing asked: every parameter has its default.

## Connectors (ports)

A port has an `id`, a `kind` and a label. `flow`: the step below waits for this one. `data`: something given to a step. A data port says what it `carries`
(`value`, `reading`, `archives`) or `accepts`; an edge joins an output to an input of the same kind that accepts what it carries. Data orders nothing, except
that a step that takes a value does not run before the step it comes from has produced it. `PORT_IN` / `PORT_OUT` in `nodes.py` are the table;
`catalogue()` carries it to the page as `ports: {in, out}` (a decision's output is `"branches"`).

| Step | Inputs | Outputs |
|---|---|---|
| any step | `in` (flow) | `out` (flow) |
| action | `in`, `data` (values), `rag` (archives) | `out`, `result` (value) |
| decision | `in`, `data`, `rag` | one `flow` port for each branch, named by the branch `id` |
| sensor | `in` | `out`, `reading` (value: the text it matched) |
| get_var, set_var | `in` (set_var also `data`) | `out`, `value` |
| input (a request to the person) | `in`, `data` | `out`, `answer` |
| start | none | `out` |
| end | `in` | none (reaching it switches the plan off, see [plan-start.md](plan-start.md)) |
| parameter | none | none (a value of the plan, set in the Parameters tab, see [plan-runs.md](plan-runs.md)) |
| set_var, output, log, wait_condition, loop | `in`, `data` | `out` |
| rag | none | `rag` (archives) |

In a diff an edge is `[parent, child]`, `[parent, child, port]` or `{from: {node, port}, to: {node}}`; the port may be left out when the parent has only one flow
port (or, for a decision stored before branches, when a branch has the child's name). Flow edges are in `dependencies` / `outputs`; a decision's and a RAG node's
edges are also in `routes: {port: [child ids]}` (a data edge only there). A value edge is stored on the step that takes it, in `inputs: {name: {node, port}}`: the
name is how the step refers to it (made from the source's title, renamed in the inspector); a value cannot come back round (`PlanError`). A step's result is
its one value, or all of them when it wrote several. `PlanDAG.edges()` lists every edge as `{from: {node, port}, to: {node, port, name?}, kind, carries?}`;
`waiting_inputs` in a step's state says which inputs have no value yet. Removing a step or a branch takes its edges and inputs with it.

## Decision

`spec`: `mode`, `on_no_match`, `branches`. A comparison branch is three terms, `{id, label, left, op, right, right2?}` (`right2` is the upper end of `between`),
each an **operand** (`tadamoo/planner/operands.py`): `{kind: value, value}`, `{kind: python, expr}` (a condition expression over the plan's values and the inputs),
`{kind: input, from: name}` (an input wired to the decision) or `{kind: input, variable: key}` (a value of the plan), or `{kind: agent, describe}`. The
comparison is `<  <=  =  !=  >=  >  between  in  contains`; an operator that is not one of them stops the decision with that message, never a guess; terms that
are not numbers cannot be ordered. A branch can also be one whole expression `{id, label, condition}`, or `{id, label, op: "else"}`. The older helper form
`{op, value, value2}` with the decision's `input` expression still makes the three terms. Labels are unique (at most 40 characters), `else` is at most one and
last, a new branch gets an id and ids stay when branches are reordered.

A term for the agent waits: completing the decision answers `waiting` with what to work out, and the agent completes it again with `outputs` of
`"<branch id>.<left|op|right|right2>": value` (a text, a number, true/false; kept on the decision, not among the plan's values). An input with no value yet
makes it wait the same way. What was compared and whether it held is kept in `state_variables.compared`.

- **`mode`**: `first` (default) takes the first branch that holds in the order shown, so overlapping ranges ("> 10", "10 to 30") do what the order says;
  `all` takes every branch that holds. The steps only the other branches lead to are skipped.
- **`else`** is taken when no other branch holds.
- **`on_no_match`** (when nothing holds and there is no else): `stop` (default) completes the decision with `state_variables.no_branch` (the conditions read, with
  values) and skips what is below it; the rest of the plan goes on and the page shows "No route". `ask` hands the decision to the person, as before
  (`state_variables.no_route`). A condition that cannot be read yet always stops the decision for the person.

A decision stored with a condition for each exit loads as `all`-mode `condition` branches named by their exits, with `on_no_match: ask` (`migrate_decision`, run when a
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
tooltip and the accessible name). Connectors are 10 px dots; the touch area is a larger, invisible one. Flow edges leave the right and enter the left; a value
leaves the bottom edge of its step (a dot) and enters the top edge at the inputs dot, labelled with the input's name; archives enter at the top centre, dashed.
Linking by drag has a twin in the inspector: a Connections section (a select and Connect for each branch or for a RAG node, a button to disconnect each, 44 px)
and an Inputs section (the inputs of a step with their names to rename, their source, a choice of one more), which is how a phone or a keyboard connects. The
branch editor shows each comparison as three rows, first term, comparison and second term (a fourth for the upper end of `between`), each with a choice of
source: a value, a python expression, an input, or a description for the agent. Type sizes: title about 13 px, branch and port labels 11 px. The archive picker
is `serve/web/ragpick.js`. `serve/tests/plan_connect_check.js` drives a real headless browser with mouse and touch pointers and asks the server whether the edge
landed, for a plain step, a branch, a RAG node and a value.

An action step may also carry `effort` (`none`, `low`, `medium`, `high`; empty follows the plan): how long the model thinks while it does that step. The plan's own
inference settings and the list that sets one level on all steps are in [inference-settings.md](inference-settings.md). Test: `tests/test_inference_scope.py`.

## Skills of a step and of a plan

An action step may carry `skills` (`spec.skills`: names of installed skills) and a plan may carry `profile.skills` (set with `plan_set_profile` or in the plan's profile tab).
`tadamoo/planner/skills.py` `of_step` gives, for a step, the plan's skills and its own (a skill in both is the plan's). When the agent runs `plan_step_start` the result
carries their instructions: the plan's under "Skills of the whole plan", the step's own under "Skills of this step (they apply to this step only)"; a skill that is not installed
or is turned off is named with a line saying so. The plan's skills are given to every step, used or not, but once for each conversation (the conversation still has them);
a step's own skills are given when that step starts. The text is read through the capability index (`skill:<name>`) and the skill catalogue (`tadamoo/session.py`, `skill_text`).

- **Where a step runs.** Today the steps for the agent of a plan are done by one conversation of the agent, shared by all of them (one routine for each plan, see
  [plan-runs.md](plan-runs.md)); the technical session only reads the waits. The instructions of a step's own skill therefore stay in that conversation's history after the
  step, and what the text says ("this step only") is all that limits them. A step that gets a conversation of its own would take the same lists at its start.
- **The picker** is `serve/web/skillpick.js` (the installed skills; a name no longer installed is shown as such).
- Test: `tests/test_planner_skills.py`.

Tests: `tests/test_planner_ports.py`, `tests/test_planner_operands.py`, `tests/test_planner_decisions.py`, `tests/test_planner_rag.py`, `serve/tests/test_plans_page.py`,
`serve/tests/plankinds_check.js`, `serve/tests/plankinds_dom_check.js`.
