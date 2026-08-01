# Youtab Advanced Computation Runtime

**Immediate Architecture for Reliable Mathematics, Physics, Scientific Reasoning, Solver-Backed Execution, Verification, and Authority-Governed Precision**

**Document intent:** This file is written as an immediate engineering architecture document for inserting into the Youtab architecture canon, runtime design, ADR backlog, or implementation roadmap. It preserves the full professional direction of the prior explanation and expands it into a buildable, layered, low-error system design.

**Core doctrine:** A language model must not be the final calculator, final physicist, final simulator, or final proof authority. The language model should understand, formalize, coordinate, explain, and interpret, while specialized solvers, proof checkers, simulators, knowledge systems, and verifiers produce the reliable computational evidence. Cognitive Authority governs the entire path.


## Core Position

For advanced mathematics, physics, and scientific problem solving inside Youtab, the correct design is not to make the language model calculate everything by itself. The professional design is to make the language model a reasoning coordinator, a formalization assistant, an explanation engine, and an interpreter of verified computational outputs.

The reliable computation must be delegated to tools designed for computation: computer algebra systems, numerical solvers, high-precision arithmetic engines, automatic differentiation engines, GPU workers, simulation engines, formal proof checkers, unit checkers, source-grounded retrieval, and independent verification layers.

The final answer should be produced only after the problem is understood, formalized, solved by the appropriate engine, verified, checked for physical and mathematical consistency, traced, and approved by the relevant Youtab governance policy.

The essential formula for Youtab Scientific Reasoning is: Authority-governed, RAG-grounded, solver-backed, CAS-verified, numerically validated, simulation-capable, proof-checkable, traceable, cost-aware, and fail-closed under uncertainty.


## Non-Negotiable Principle

A language model can appear confident while making arithmetic, algebraic, numerical, physical, or logical mistakes. Therefore, Youtab must never treat the language model's raw generated answer as the final scientific truth for serious mathematics or physics.

The model may propose a route, identify laws, select equations, explain steps, and translate outputs into human language. However, computation, verification, proof checking, simulation, and high-risk scientific validation must be performed by dedicated engines and controlled by Cognitive Authority.

The low-error path is: formalize the problem, select the method, execute with specialized tools, verify the result, cross-check when risk requires it, explain with assumptions and limitations, and preserve trace evidence.


## Youtab System Alignment

This architecture is designed to fit Youtab's One Brain plus N Engines doctrine. There is one Cognitive Authority that governs the route, risk, cost, permission, and final admissibility of the result. There are many replaceable engines under it: models, solvers, proof checkers, simulators, retrieval systems, and verifiers.

The Scientific Runtime must not become a second brain. It is a governed computational runtime. It executes mathematical, physical, simulation, proof, and verification tasks under Authority-defined permissions, risk gates, cost gates, and trace requirements.

The Agent Runtime remains tactical. Math Agents, Physics Agents, Simulation Agents, Verification Agents, and Citation Agents may execute delegated tasks, but they do not own sovereignty. They do not decide final admissibility. They report evidence upward to Cognitive Authority.


## Top-Level Runtime Flow

The full runtime flow begins when a user intent enters Youtab. Cognitive Authority classifies the request, chooses the risk and cost tier, and delegates to a workflow. The workflow invokes agents and tools. The tools solve, simulate, retrieve, or verify. The verifier checks results. Trace records all important decisions, inputs, outputs, assumptions, and validation steps. Cognitive Authority then decides whether the answer is allowed, conditional, incomplete, or blocked.

The preferred operational flow is: User Intent enters Cognitive Authority; Cognitive Authority applies computation policy; the Math or Physics Workflow classifies and formalizes the problem; the Runtime selects engines; the engines execute in a sandbox; Verification checks the result; Knowledge and Trace record evidence; the final answer is produced with assumptions, method, result, limitations, and confidence.


## Required Layers

- Cognitive Authority Layer for routing, admissibility, cost, risk, permissions, escalation, and final decision control.

- Computation Policy Layer for deciding which scientific route is required for the request.

- Problem Understanding Layer for parsing the user question, scientific intent, scope, and expected output.

- Domain Classification Layer for identifying whether the problem is algebra, calculus, probability, statistics, mechanics, electromagnetism, thermodynamics, quantum mechanics, relativity, PDE, CFD, optimization, simulation, formal proof, or another domain.

- Formalization Layer for converting natural language into variables, equations, constraints, assumptions, units, domains, initial conditions, boundary conditions, and output requirements.

- Scientific Knowledge and RAG Layer for retrieving formulas, definitions, constants, books, papers, standards, and validated scientific references.

- Symbolic Computation Layer for exact algebra, calculus, simplification, equation solving, symbolic transformations, and symbolic verification.

- Numerical Computation Layer for numerical solving, optimization, integration, linear algebra, differential equations, interpolation, eigenvalue problems, sparse systems, and statistics.

- High-Precision Arithmetic Layer for cases where floating-point instability, cancellation, tiny differences, ill-conditioning, or high accuracy requirements matter.

- Automatic Differentiation and Accelerator Layer for gradient-heavy optimization, inverse problems, differentiable physics, scientific machine learning, and GPU or TPU execution.

- Simulation Layer for PDE, FEM, CFD, multiphysics, robotics, multibody dynamics, discrete-event simulation, and research-grade computational physics.

- Formal Proof Layer for machine-checkable mathematical proof, program correctness, invariant verification, and high-assurance reasoning.

- Unit and Dimensional Analysis Layer for detecting impossible physical expressions, unit mismatches, scale errors, and hidden assumptions.

- Verification Layer for residual checks, domain checks, boundary checks, convergence checks, stability checks, sensitivity checks, independent solver checks, and source checks.

- Trace and Audit Layer for recording how the answer was produced, which tools ran, what assumptions were made, what evidence was used, and what validation passed or failed.

- Cost Governor Layer for starting cheap, escalating only when needed, and preventing expensive computation for simple tasks.

- Human Review and Fail-Closed Layer for critical scientific, industrial, legal, medical, safety, infrastructure, or irreversible decisions.


## Cognitive Authority Responsibilities

- Decide whether a question is conceptual, computational, simulation-grade, proof-grade, safety-critical, or incomplete.

- Select the cheapest safe path first, then escalate only when the problem requires more precision, more verification, or more compute.

- Prevent the language model from returning unverified numerical or scientific conclusions when the task requires solver-backed output.

- Require explicit assumptions when user data is incomplete.

- Require source grounding when formulas, constants, laws, or scientific claims are used.

- Require unit checks for physical problems.

- Require residual, convergence, stability, and sensitivity checks when numerical results are used.

- Require independent verification for sensitive, expensive, high-risk, or research-grade work.

- Escalate to human review for critical decisions, real-world engineering use, safety consequences, or irreversible actions.

- Fail closed when the system cannot produce adequate evidence.


## Role of the Language Model

- Understand the user request and ask clarifying questions when missing details materially affect correctness.

- Classify the scientific domain and identify the likely solution family.

- Convert natural language into a formal mathematical or physical problem statement.

- Choose candidate methods and route them to the appropriate computational engine.

- Generate solver calls or executable analysis code under sandbox constraints.

- Interpret the raw output of solvers, simulators, and proof checkers.

- Explain the solution in a human-readable way.

- Identify assumptions, limitations, uncertainty, and domain restrictions.

- Never become the sole authority for high-stakes computation.


## Role of RAG and Scientific Knowledge

RAG is the source-grounding layer. It is not a calculator. It retrieves validated formulas, definitions, constants, derivations, textbooks, papers, standards, benchmark problems, and domain-specific references. For physics and mathematics, RAG must support both semantic retrieval and exact retrieval because formula names, theorem names, equation numbers, constants, variables, and symbols often require exact matching.

The RAG layer should attach metadata to each scientific source: domain, source type, author, edition or version, page or section, equation identifier, validity conditions, assumptions, units, confidence, and whether the source is primary, secondary, educational, or user-provided.

RAG should not silently override solver evidence. It provides source context. Solvers perform computation. Verifiers check correctness. Authority decides admissibility.


## Symbolic Computation Layer

The symbolic layer is required for exact mathematics. It should be used before approximate numerical computation whenever a closed-form or exact symbolic solution is feasible. Symbolic computation is essential for algebraic simplification, solving equations, differentiation, integration, limits, symbolic linear algebra, exact transformations, and checking whether a proposed expression satisfies a mathematical statement.

A practical open-source starting point is SymPy, which is a Python library for symbolic mathematics and aims to be a full-featured computer algebra system while remaining simple and extensible. For more advanced or proprietary workflows, Wolfram Language, Mathematica, Maple, or SageMath can be added as optional engines.

The symbolic layer should not be trusted blindly either. Outputs must be checked by substitution, domain validation, branch-condition validation, singularity checks, and comparison against special cases.


## Numerical Computation Layer

The numerical layer handles problems where exact symbolic solutions are impossible, too expensive, unnecessary, unstable, or not available. It should support root finding, optimization, integration, interpolation, eigenvalue problems, differential equations, sparse systems, statistics, and numerical linear algebra.

The baseline stack should include NumPy, SciPy, and mpmath. SciPy provides algorithms for optimization, integration, interpolation, eigenvalue problems, algebraic equations, differential equations, statistics, and many other scientific computing problems. mpmath should be used for high-precision arithmetic when ordinary floating-point precision is insufficient.

Numerical outputs must include verification. Youtab should compute residuals, convergence status, tolerance, absolute error when available, relative error when available, condition number when relevant, and sensitivity to initial conditions or input perturbations when needed.


## Automatic Differentiation and Accelerator Layer

Advanced physics, optimization, inverse modeling, differentiable simulation, scientific machine learning, parameter estimation, and gradient-heavy workloads require automatic differentiation and accelerator support. JAX is an appropriate advanced engine because it supports composable transformations, automatic differentiation, just-in-time compilation, vectorization, batching, and execution on accelerators.

This layer should not be used for simple questions. It should be activated when gradients, high-dimensional optimization, GPU acceleration, differentiable physics, large batched computations, or inverse problems require it. PyTorch can be used when the workload is more machine-learning-heavy, while JAX is especially strong for numerical transformations and differentiable scientific computing.

The accelerator layer must be sandboxed, quota-controlled, cost-controlled, and traceable. Expensive GPU jobs should not run just because a user asks a simple conceptual question.


## Simulation Layer

Physics becomes serious when the problem is not reducible to a simple formula. PDEs, FEM, CFD, heat transfer, electromagnetism, solid mechanics, fluid dynamics, robotics, multiphysics systems, and time-dependent models require simulation engines. The simulation layer exists for these cases.

For PDE and finite element workflows, FEniCS is a strong open-source option because it is designed for solving partial differential equations with the finite element method and can translate scientific models into efficient finite element code. For CFD, OpenFOAM is a strong option. For robotics and multibody dynamics, MuJoCo or PyBullet can be integrated. For system modeling, OpenModelica can be considered. For discrete-event simulation, SimPy can be used.

Simulation must never be treated as automatically true. It must be validated through mesh convergence, time-step convergence, residual monitoring, stability checks, comparison against analytical special cases, benchmark problems, conservation laws, and uncertainty analysis. A beautiful simulation without validation is not scientific proof.


## Formal Proof Layer

Some mathematical claims require proof rather than computation. For high-assurance proofs, algorithm correctness, invariants, safety-critical properties, and formal mathematical statements, Youtab should integrate a proof assistant such as Lean, Rocq or Coq, or Isabelle/HOL.

The language model may propose a proof sketch, translate a theorem into a formal statement, attempt proof scripts, and explain the result. But the proof checker must be the authority for whether the formal proof is accepted. Youtab should not say that a theorem has been formally proved unless the proof checker accepts it.

For ordinary educational answers, formal proof may be unnecessary. For research-grade or safety-sensitive claims, it becomes valuable.


## Verification Layer

The Verification Layer is the heart of low-error scientific reasoning. It must be independent enough to challenge the solver output and model explanation. A solver produces a candidate answer. The verifier asks whether that answer satisfies the original problem, the assumptions, the domain, the units, the boundary conditions, the numerical tolerance, and the physical laws.

For algebraic problems, verification includes substitution, simplification, identity checks, domain checks, singularity checks, and detection of extraneous solutions. For numerical problems, verification includes residuals, convergence, condition numbers, tolerance status, independent methods, and sensitivity analysis. For physics problems, verification includes dimensional analysis, conservation laws, order-of-magnitude checks, boundary conditions, initial conditions, sign checks, physical range checks, and limiting cases.

The verifier should not only check success. It should produce a validation report. The final answer should disclose limitations when validation is partial, approximate, or conditional.


## Trace and Audit Layer

Youtab must record scientific computation traces. A trace should include the user question, interpreted intent, assumptions, extracted variables, units, selected domain, selected method, retrieved sources, tool calls, solver versions when available, raw outputs, verification checks, failures, retries, final result, and confidence or limitation notes.

Trace makes the answer inspectable. Without trace, a scientific answer is only a claim. With trace, the answer becomes auditable, reproducible, and debuggable.

For sensitive tasks, trace should be immutable or tamper-evident according to Youtab's broader audit architecture.


## Cost-Aware Tiering for Youtab

Youtab must not route every question to the most expensive computation path. The system should start with the cheapest safe path and escalate only when the task, risk, ambiguity, or requested precision requires it. This preserves speed, cost efficiency, and user experience while still allowing very deep computation for serious work.

The user-facing free chat tier should be the base path. Advanced mathematical and physical computation should begin from the next Youtab tier and escalate through higher levels for sensitive, difficult, or research-grade tasks. The earlier internal scientific levels remain valid, but they must be mapped to Youtab product levels carefully.


## Youtab Free Chat Tier

The free chat tier should support conceptual explanations, simple definitions, basic formulas, lightweight RAG, and very light computation. It should be fast and cheap. It should not run heavy solvers, GPU workers, formal proof engines, CFD, FEM, or expensive simulations by default.

This tier can answer basic educational questions, explain concepts, and provide simple symbolic or arithmetic checks when safe. If a task requires serious computation, it should explain that a higher scientific computation tier is needed or offer a simplified approximation.


## Youtab Advanced Tier

The first paid or advanced tier should activate standard solver-backed mathematics and physics. This includes SymPy, NumPy, SciPy, mpmath, unit checking, formula retrieval, and standard verification. It should handle most university-level homework, engineering basics, calculus, algebra, differential equations, standard mechanics, thermodynamics, electromagnetism basics, and ordinary numerical analysis.

This tier should be the first tier where Youtab behaves like a serious computational assistant rather than only a conversational tutor.


## Youtab Professional Tier

The professional tier should activate stronger verification, higher precision arithmetic, multi-method checking, sensitivity analysis, better scientific RAG, larger contexts, larger file processing, and more reliable workflow orchestration. It should be suitable for professional users, researchers, students working on advanced material, and engineers who need more than basic answers.

This tier should include independent solver verification for tasks where a single method is insufficient. It should be able to say when symbolic, numerical, and approximate methods disagree.


## Youtab Expert Tier

The expert tier should support advanced numerical computation, optimization, automatic differentiation, GPU-backed JAX jobs, larger scientific workflows, batch computations, advanced ODE and PDE routing, and deeper validation reports.

This tier should be used when problems are complex, high-dimensional, gradient-heavy, compute-heavy, or require advanced numerical methods beyond standard educational solving.


## Youtab Research Tier

The research tier should support PDE, FEM, CFD, simulation job management, mesh and time-step convergence, scientific visualization, benchmarking, reproducibility packages, formal proof attempts, and multi-engine verification.

This tier is where the most complex and sensitive scientific workflows begin to become realistic. It should not promise certainty. It should provide validated, traceable, reproducible, and limitation-aware computation.


## Youtab Critical Tier

The critical tier is for high-risk, industrial, infrastructure, safety, medical, legal, financial, aerospace, factory, or irreversible decisions. This tier should require the strongest verification, independent methods, human review, audit trace, explicit uncertainty, and fail-closed behavior.

In this tier, Youtab should not act as an autonomous final authority for real-world safety-critical control. It can analyze, simulate, advise, verify, and prepare evidence, but final critical decisions require appropriate human or institutional approval.


## Professional Problem-Solving Pipeline

- Understand the user intent and requested output.

- Classify the domain and subdomain.

- Extract data, variables, units, unknowns, constraints, initial conditions, boundary conditions, and assumptions.

- Detect missing information and either ask a clarifying question or proceed with explicit assumptions when safe.

- Select the method: symbolic, numerical, approximate, simulation, proof, retrieval, or hybrid.

- Select tools according to risk, precision, cost, and domain.

- Execute computations in a sandboxed and traceable environment.

- Verify using algebraic, numerical, physical, source, and independent-method checks.

- Escalate when results are unstable, ambiguous, contradictory, or high-risk.

- Explain the answer with method, assumptions, result, limitations, and confidence.

- Store trace evidence for audit, debugging, repeatability, and future improvement.


## Low-Error Route

The lowest-error route is not one method. The lowest-error route is method selection plus verification plus cross-checking when the problem requires it. For simple tasks, one safe solver path may be enough. For sensitive tasks, multiple independent methods are required.

The general low-error route is: formalize the problem, solve using the most appropriate engine, verify the result, compare against a second route when needed, check units and domains, report assumptions and limitations, and preserve trace evidence.


## Multiple-Method Doctrine

A single method is not enough for sensitive scientific tasks. A symbolic answer should be checked by substitution or a numerical special case. A numerical answer should be checked by residual and convergence. A simulation should be checked against analytical limits or benchmarks. A proof sketch should be checked by a proof assistant when formal certainty is claimed. A formula retrieved from RAG should be checked against source metadata and applicability conditions.

This is a core design choice for Youtab: precision comes from controlled redundancy, not blind confidence. Multiple methods are not waste when risk is high; they are the mechanism that prevents confident wrong answers.


## Mathematics-Specific Standards

- Define all variables, domains, and assumptions before solving.

- Check whether a solution exists and whether it is unique.

- Track real versus complex domains.

- Detect branch cuts, singularities, discontinuities, and undefined points.

- Avoid illegal algebraic transformations such as division by an expression that may be zero.

- Detect extraneous solutions introduced by squaring, substitution, or transformations.

- Check all roots, not only the first root returned by a solver.

- Use exact symbolic methods when feasible.

- Use high-precision numerical methods when conditioning requires it.

- Report approximation error when an exact answer is not available.


## Physics-Specific Standards

- Identify the physical regime before applying formulas.

- State whether the model is classical, relativistic, quantum, thermodynamic, continuum, discrete, linear, nonlinear, steady-state, transient, idealized, or empirical.

- State all assumptions such as no friction, no air resistance, point mass, rigid body, incompressibility, steady flow, thermal equilibrium, small angle approximation, or nonrelativistic speed.

- Check units and dimensions before and after computation.

- Check conservation laws when applicable: energy, momentum, angular momentum, charge, mass, probability, or other conserved quantities.

- Check boundary conditions and initial conditions.

- Check limiting cases to see whether the result behaves correctly when parameters become small, large, zero, or symmetric.

- Report the model's domain of validity.

- Separate physical law from numerical method.

- Separate result from interpretation.


## Simulation-Specific Standards

- State the governing equations and physical model.

- State the discretization method, solver family, mesh, time step, convergence criteria, and tolerance.

- Check mesh independence or grid convergence when the result depends on discretization.

- Check time-step convergence for transient simulations.

- Compare against analytical solutions when available.

- Compare against benchmark problems when analytical solutions are unavailable.

- Monitor residuals and conservation laws.

- State computational limitations, model limitations, and uncertainty.

- Keep simulation configuration and results reproducible.

- Do not treat visualization as validation.


## Formal Proof Standards

- Distinguish between an informal proof sketch and a machine-checked proof.

- Only call a proof formally verified when a proof assistant accepts it.

- Preserve theorem statement, assumptions, definitions, imported libraries, proof script, checker output, and failure logs.

- Use formal proof for critical algorithms, invariants, safety properties, and mathematical claims where proof-level assurance is required.

- Explain the machine-checked result in human language after the checker validates it.


## Recommended Technical Stack

- Python as the core scientific execution language.

- SymPy for symbolic mathematics and baseline computer algebra.

- NumPy for arrays and numerical foundations.

- SciPy for scientific algorithms including optimization, integration, interpolation, eigenvalue problems, algebraic equations, differential equations, statistics, and related problems.

- mpmath for arbitrary-precision floating-point arithmetic and high-precision numerical checks.

- Pint or an equivalent units library for unit and dimensional analysis.

- JAX for automatic differentiation, just-in-time compilation, vectorization, batching, accelerator execution, and differentiable scientific computation.

- PyTorch when machine-learning-heavy scientific workflows require it.

- Matplotlib or a controlled visualization layer for plots and scientific visual outputs.

- FEniCS for finite element PDE solving.

- OpenFOAM for CFD workflows.

- MuJoCo or PyBullet for robotics, contact dynamics, and multibody simulation.

- OpenModelica for system modeling when appropriate.

- Lean as the first formal proof assistant candidate, with Rocq or Coq and Isabelle/HOL as additional options.

- Hybrid RAG with vector search plus keyword search for formulas, theorem names, equation references, symbols, constants, and exact citations.

- Sandboxed workers for all executable computation.

- Trace logging for every serious computation.

- Cost governance for proportional compute.


## Tool Routing Policy

- Conceptual explanation should use the language model and lightweight RAG.

- Formula lookup should use RAG plus exact keyword search and source metadata.

- Algebra, calculus, simplification, exact solving, and symbolic verification should use CAS.

- Numerical roots, optimization, integration, ODEs, matrices, sparse systems, and statistics should use SciPy and mpmath when needed.

- Gradient-heavy optimization and inverse problems should use JAX.

- GPU-heavy or batched scientific computation should use accelerator-backed workers.

- PDE and FEM problems should route to FEniCS or a domain-appropriate PDE engine.

- CFD problems should route to OpenFOAM or a CFD-specific engine.

- Formal theorem proving should route to Lean or another proof assistant.

- Critical work should require multiple independent checks and human review.


## Implementation Roadmap

- Start with the core math tool API, expression parser, SymPy runner, SciPy runner, mpmath runner, unit checker, and trace logger.

- Add formula RAG, constants database, physics domain classifier, assumption manager, and dimensional analysis.

- Add verification reports for residuals, units, domains, convergence, sensitivity, and independent-method checking.

- Add advanced numerical runtime with JAX, GPU workers, job queues, quotas, and sandboxed execution.

- Add simulation adapters for PDE, FEM, CFD, robotics, and scientific visualization.

- Add formal proof adapters for Lean and optional additional proof assistants.

- Add critical-mode governance with multi-solver verification, human review, reproducibility packages, and tamper-evident trace.


## Immediate Architecture Module Names

- ScientificRuntime

- ComputationAuthorityPolicy

- MathProblemClassifier

- PhysicsProblemClassifier

- ProblemFormalizer

- AssumptionManager

- ScientificRAG

- FormulaRetriever

- ConstantsRegistry

- SymbolicSolverAdapter

- NumericalSolverAdapter

- HighPrecisionRunner

- UnitDimensionChecker

- AutodiffAcceleratorRunner

- SimulationRuntime

- FormalProofRuntime

- VerificationRuntime

- ComputationTraceLedger

- CostGovernor

- HumanReviewGate


## Critical Failure Modes to Prevent

- The language model gives a final numerical answer without using a solver when solver-backed computation is required.

- The system retrieves a formula but applies it outside its domain of validity.

- The system ignores units or mixes incompatible units.

- The system assumes missing parameters without disclosure.

- A numerical solver returns a value but no residual or convergence check is performed.

- A simulation produces a visualization but no validation is performed.

- A proof sketch is presented as a formal proof without machine checking.

- An expensive computation is run for a simple request.

- A high-risk answer is delivered without human review or fail-closed behavior.

- Trace is missing, incomplete, or not reproducible.


## Quality Gates

- The problem has been formalized clearly enough to solve.

- Assumptions are explicit.

- The method matches the domain and risk level.

- The computation was performed by an appropriate engine.

- Units and dimensions were checked for physical problems.

- The result was verified by residual, substitution, convergence, stability, or another appropriate check.

- Sensitive results were cross-checked with an independent method.

- Sources were retrieved and cited when scientific formulas, constants, or references were used.

- Limitations and uncertainty were disclosed.

- Trace evidence exists.


## Final Doctrine

Youtab's advanced computation system should turn the language model into a leader of scientific reasoning, not a fragile calculator. The leader understands the mission, chooses the correct path, delegates to specialized engines, interprets evidence, explains the result, and remains governed by Cognitive Authority.

The highest accuracy comes from layered intelligence: source grounding, formalization, symbolic solving, numerical solving, simulation, proof checking, verification, trace, and risk-based governance. No single method is enough for sensitive work. The system becomes strong precisely because it knows when one method is insufficient.

The target architecture is a complete scientific reasoning organism: one Cognitive Authority, many computational engines, controlled agents, explicit workflows, auditable traces, proportional cost, and fail-closed safety. This is the path that makes Youtab's brain a precise, intelligent, professional leader rather than a text generator pretending to compute.


## References for Engineering Grounding

- SymPy official site: SymPy is a Python library for symbolic mathematics and aims to become a full-featured computer algebra system.

- SciPy official site and documentation: SciPy provides scientific algorithms for optimization, integration, interpolation, eigenvalue problems, algebraic equations, differential equations, statistics, and other classes of problems.

- JAX official documentation: JAX supports composable transformations for compilation, batching, automatic differentiation, vectorization, and parallelization.

- FEniCS official site: FEniCS is an open-source computing platform for solving partial differential equations using the finite element method.

- Lean official site: Lean is an open-source proof assistant and programming language for formally verified reasoning.

- Rocq/Coq official site: Rocq/Coq is an interactive theorem prover and proof assistant for formal specifications and machine-checked proofs.
