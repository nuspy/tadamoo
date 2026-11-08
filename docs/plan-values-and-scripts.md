# Values in a plan, and a plan as a script

## How a value gets from one step to another

A plan has one set of values, shared by all its steps (`shared_state`). Information does not travel along the arrows; the arrows say only the order.

1. A step that finishes writes values with `plan_mark ... outputs {name: value}`; the plan stores them under those names.
2. A step can say what it produces (`produces`: a list of `name:type`, with number, text, bool, list, object or any). A step that says so cannot be
   completed without those values, of those types: the refusal names what is missing.
3. When the agent starts a step, `plan_step_start` hands it the step's description with every `{name}` replaced by the value, and lists the values the steps
   directly above it wrote and the plan values its description names. Nothing depends on the model having read the plan first.
4. A step that must come after several others lists all of them as inputs: a step takes any number of inputs and gives any number of outputs.

## Set value and Read value

| Step | Fields | What it does |
| --- | --- | --- |
| Set value (`set_var`) | `name`, `value` or `expr`, `target` | Writes a value into this plan (`plan`) or into the values shared by all plans (`global`, one JSON file in the planner folder). |
| Read value (`get_var`) | `name`, `into`, `source`, `step` | Reads a plan value (`variable`), the clock (`clock`: now, date, time, weekday, hour, timestamp), a shared value (`global`) or what another step wrote (`step`). It writes it under `into`, or its own name. |

A read of a value that is not there yet waits and says so.

## A plan that ended well, as a script

When every step of a plan is completed or skipped, the Plans page shows **Turn into a script**. It makes a personal skill, a Python script run again with other values, with the same code
as a conversation does (`tadamoo/personal_skills/distill.py`); the plan adds `tadamoo/personal_skills/from_plan.py`.

- **The record.** The tool calls of each step are found in the conversation that ran it, between the step's start and its completion. The plan's own calls and the failed
  tries are left out.
- **What each step is.** Recorded calls; words only (a choice, a judgement: a call to the model, `ai()`, with exactly the text it needs); asks the person (`ask()`); worked out
  by the plan itself (waits, values, decisions, results); or nothing recorded.
- **Steps with nothing recorded** are never guessed. The model proposes the calls, and the person approves them one by one, or leaves the step to the model. The script is not
  written until every such step is decided. Approved calls are written into the script but not run.
- **The script is plain code.** One function for each step, taking the values it reads as arguments and returning the values it produces; `main(params)` calls them in
  the plan's order; a decision is an `if` on those values.
- **The parameters** are the values that change between runs, found without the model where possible. The plan's questions to the person are parameters as they are.
  They are changed in a form and by talking to the model ("make the folder a parameter and drop the date").
- **The result** is saved as a personal skill, with the plan and the conversation recorded as its source.
