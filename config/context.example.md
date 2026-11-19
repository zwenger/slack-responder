<!-- TEMPLATE: the guided setup rewrites this file. Remove this line once you edit it by hand. -->
# Context for the responder

Everything here is given to the agent that investigates and answers. Write it for a new colleague.

## Company and product
What the company does, the main apps, and who usually asks questions (CS, ops, sales…).

## How to investigate
How to use each tool listed in `config/responder.toml` → `[investigation]`: what the command does,
its exact syntax, and its limits (for example "inline SQL must end with `;`").

## What people can do themselves
Admin tools or settings that let the asker fix things without a developer, with their URLs and where
in the code to confirm that an action exists.

## Never
Things the agent must never say or promise.
