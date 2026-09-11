// SPDX-License-Identifier: BUSL-1.1
// SPDX-FileCopyrightText: © 2026 INTERCHAINED LLC × Claude Sonnet 4.6

//! The execution plan — what the evaluator actually did, and how to render it.
//!
//! # Why this exists
//!
//! Before this, parsing, semantics, optimisation and execution all lived in one
//! growing function. That is survivable while there is one execution strategy
//! and no rewrites; it stops being survivable the moment a query can be run
//! more than one way, because there is then no place to record WHICH way was
//! chosen or WHY.
//!
//! This is deliberately not a PostgreSQL planner. There is no cost model, no
//! statistics, and no search over join orders. It is a record of the pipeline
//! that ran, with the real row counts it moved.
//!
//! # The plan is EMITTED by execution, never written alongside it
//!
//! Every node here is appended by the executor as it does the work, and the
//! row counts are the counts it actually observed. That is a design constraint,
//! not an implementation detail: a plan assembled independently of the executor
//! can drift out of agreement with it, and an `EXPLAIN` that confidently
//! describes a pipeline the engine did not run is worse than having no
//! `EXPLAIN` at all — it sends the reader to optimise a query shape that never
//! existed.
//!
//! For the same reason it is stored as a PIPELINE (a `Vec` of stages) rather
//! than a tree: the executor is a pipeline — materialise, join, filter,
//! project, sort, paginate — and a tree structure would imply a generality it
//! does not have.
//!
//! The one exception is a join, which genuinely has two inputs, and
//! [`Plan::render`] accounts for that by printing them as siblings. Getting
//! that wrong is not cosmetic: the first version indented the two scans
//! differently, which reads as "the left relation was scanned inside the scan
//! of the right one" — a claim about the execution that was simply false.
//!
//! # Consequently, `EXPLAIN` here always reports actual rows
//!
//! PostgreSQL's bare `EXPLAIN` estimates without executing, and `EXPLAIN
//! ANALYZE` executes and reports reality. NEDB has no statistics to estimate
//! from, so an estimate would be a guess dressed as a number. It executes and
//! reports what happened. Stated in the output so nobody mistakes one for the
//! other.

use crate::sqljoin::Strategy;
use crate::sqlselect::JoinKind;

/// One stage of the pipeline that ran.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Stage {
    /// A base relation was materialised.
    Scan {
        table: String,
        /// The name its columns are addressed by — the alias when one was
        /// given. Shown because a plan that does not name its bindings is
        /// unreadable for a self-join.
        binding: String,
        rows: usize,
    },
    /// Two inputs were joined.
    Join {
        kind: JoinKind,
        table: String,
        binding: String,
        strategy: Strategy,
        /// Equality key pairs the planner could PROVE usable. Zero means the
        /// hash path was unavailable, which is the single most useful number
        /// in the plan when a join is unexpectedly slow.
        keys: usize,
        left_rows: usize,
        right_rows: usize,
        out_rows: usize,
        /// The join stopped early because the row budget was already met.
        early_stopped: bool,
    },
    /// A `WHERE` clause was applied.
    Filter { in_rows: usize, out_rows: usize },
    /// The select list was evaluated.
    Project { columns: usize, out_rows: usize },
    /// `DISTINCT` removed duplicates.
    Distinct { in_rows: usize, out_rows: usize },
    /// `ORDER BY` sorted the rows.
    Sort { keys: usize, rows: usize },
    /// `LIMIT` / `OFFSET` were applied.
    Limit {
        limit: Option<usize>,
        offset: Option<usize>,
        in_rows: usize,
        out_rows: usize,
    },
}

/// What ran, in the order it ran.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Plan {
    pub stages: Vec<Stage>,
    /// Set when the row budget let a stage stop before consuming its input.
    pub budget: Option<usize>,
}

impl Plan {
    pub fn push(&mut self, s: Stage) {
        self.stages.push(s);
    }

    /// The join stages, for callers that only care how joins were executed.
    ///
    /// Keeps the differential tests and the benchmark reading the same report
    /// the plan is built from, rather than a second source of truth.
    pub fn joins(&self) -> Vec<&Stage> {
        self.stages
            .iter()
            .filter(|s| matches!(s, Stage::Join { .. }))
            .collect()
    }

    /// The strategy of the nth join, if there is one.
    pub fn join_strategy(&self, n: usize) -> Option<Strategy> {
        match self.joins().get(n) {
            Some(Stage::Join { strategy, .. }) => Some(*strategy),
            _ => None,
        }
    }

    /// The strategy of every join, in execution order.
    pub fn join_strategies(&self) -> Vec<Strategy> {
        self.stages
            .iter()
            .filter_map(|s| match s {
                Stage::Join { strategy, .. } => Some(*strategy),
                _ => None,
            })
            .collect()
    }

    /// Proven equality key count of the nth join.
    pub fn join_keys(&self, n: usize) -> Option<usize> {
        match self.joins().get(n) {
            Some(Stage::Join { keys, .. }) => Some(*keys),
            _ => None,
        }
    }

    /// Render as `EXPLAIN` output: one string per line, innermost first, the
    /// way PostgreSQL nests its plan tree.
    ///
    /// The pipeline is linear, so indentation grows monotonically. A reader
    /// familiar with PostgreSQL's output will read this correctly; a reader who
    /// is not still sees the order things happened in.
    pub fn render(&self) -> Vec<String> {
        let mut out = vec![];
        let mut depth = 0usize;

        // Walked BACKWARDS: the last stage to run is the outermost operation,
        // which is what PostgreSQL prints first and least indented. Assigning
        // depth in execution order and reversing afterwards gets the
        // indentation exactly inside out.
        //
        // A join is the one stage with TWO inputs, so it is the one place the
        // pipeline is really a tree. Its right-hand scan is emitted at the SAME
        // depth as its left input rather than one deeper, because they are
        // siblings — printing them at different depths reads as "pg_class was
        // scanned inside the scan of pg_namespace", which is not what happened.
        //
        // The two inputs are listed inner-side-first, where PostgreSQL lists
        // the outer side first. The indentation and the relation names make it
        // unambiguous, and matching PostgreSQL's ordering would need a real
        // tree walk for no gain in clarity.
        let rev: Vec<&Stage> = self.stages.iter().rev().collect();
        let mut i = 0usize;
        while i < rev.len() {
            let stage = rev[i];
            i += 1;
            let indent = "  ".repeat(depth);
            let arrow = if depth == 0 { String::new() } else { format!("{indent}-> ") };
            let line = match stage {
                Stage::Scan { table, binding, rows } => {
                    let as_ = if binding == table {
                        String::new()
                    } else {
                        format!(" {binding}")
                    };
                    format!("{arrow}Seq Scan on {table}{as_}  (actual rows={rows})")
                }
                Stage::Join {
                    kind,
                    table,
                    binding,
                    strategy,
                    keys,
                    left_rows,
                    right_rows,
                    out_rows,
                    early_stopped,
                } => {
                    let as_ = if binding == table {
                        String::new()
                    } else {
                        format!(" {binding}")
                    };
                    let k = match keys {
                        0 => "no equality key".to_string(),
                        1 => "1 hash key".to_string(),
                        n => format!("{n} hash keys"),
                    };
                    let stop = if *early_stopped { ", stopped early" } else { "" };
                    format!(
                        "{arrow}{strategy} {} Join on {table}{as_} \
                         ({k}, left={left_rows}, right={right_rows}{stop}) \
                         (actual rows={out_rows})",
                        kind_name(*kind)
                    )
                }
                Stage::Filter { in_rows, out_rows } => {
                    format!("{arrow}Filter  (removed {}) (actual rows={out_rows})",
                        in_rows.saturating_sub(*out_rows))
                }
                Stage::Project { columns, out_rows } => {
                    format!("{arrow}Project  ({columns} columns) (actual rows={out_rows})")
                }
                Stage::Distinct { in_rows, out_rows } => {
                    format!("{arrow}Unique  (removed {}) (actual rows={out_rows})",
                        in_rows.saturating_sub(*out_rows))
                }
                Stage::Sort { keys, rows } => {
                    format!("{arrow}Sort  ({keys} key(s)) (actual rows={rows})")
                }
                Stage::Limit { limit, offset, in_rows, out_rows } => {
                    let l = limit.map(|n| n.to_string()).unwrap_or_else(|| "ALL".into());
                    let o = offset.map(|n| format!(", offset {n}")).unwrap_or_default();
                    format!("{arrow}Limit  ({l}{o}, from {in_rows}) (actual rows={out_rows})")
                }
            };
            out.push(line);
            depth += 1;

            // A join's two inputs are siblings. The stage immediately before
            // a join in execution order is its right-hand scan, so emit that
            // now at the depth the LEFT input will also get.
            if matches!(stage, Stage::Join { .. }) {
                if let Some(Stage::Scan { table, binding, rows }) = rev.get(i).copied() {
                    let as_ = if binding == table {
                        String::new()
                    } else {
                        format!(" {binding}")
                    };
                    out.push(format!(
                        "{}-> Seq Scan on {table}{as_}  (actual rows={rows})",
                        "  ".repeat(depth)
                    ));
                    i += 1;
                }
            }
        }

        if let Some(b) = self.budget {
            out.push(format!(
                "Row budget: {b} — the join was allowed to stop once this many \
                 rows existed"
            ));
        }
        out.push(
            "NEDB reports ACTUAL rows, never estimates: it has no statistics to \
             estimate from, and a guess printed as a number is worse than the truth."
                .to_string(),
        );
        out
    }
}

fn kind_name(k: JoinKind) -> &'static str {
    match k {
        JoinKind::Inner => "Inner",
        JoinKind::Left => "Left",
        JoinKind::Right => "Right",
        JoinKind::Full => "Full",
        JoinKind::Cross => "Cross",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scan(t: &str, rows: usize) -> Stage {
        Stage::Scan { table: t.into(), binding: t.into(), rows }
    }

    #[test]
    fn an_empty_plan_still_explains_itself() {
        let p = Plan::default();
        let r = p.render();
        assert_eq!(r.len(), 1);
        assert!(r[0].contains("ACTUAL rows"));
    }

    #[test]
    fn a_scan_renders_with_actual_rows() {
        let mut p = Plan::default();
        p.push(scan("orders", 1000));
        let r = p.render();
        assert!(r[0].starts_with("Seq Scan on orders"), "{:?}", r[0]);
        assert!(r[0].contains("actual rows=1000"));
    }

    #[test]
    fn an_alias_is_shown_but_a_redundant_one_is_not() {
        let mut p = Plan::default();
        p.push(Stage::Scan { table: "orders".into(), binding: "o".into(), rows: 1 });
        assert!(p.render()[0].contains("orders o"));

        let mut p = Plan::default();
        p.push(scan("orders", 1));
        assert!(!p.render()[0].contains("orders orders"));
    }

    #[test]
    fn the_outermost_stage_is_printed_first() {
        let mut p = Plan::default();
        p.push(scan("orders", 100));
        p.push(Stage::Limit { limit: Some(5), offset: None, in_rows: 100, out_rows: 5 });
        let r = p.render();
        assert!(r[0].starts_with("Limit"), "{r:?}");
        assert!(r[1].contains("Seq Scan"), "{r:?}");
        // The input is indented under the operation that consumes it.
        assert!(r[1].starts_with("  -> "), "{:?}", r[1]);
    }

    #[test]
    fn a_join_names_its_strategy_and_key_count() {
        let mut p = Plan::default();
        p.push(scan("orders", 1000));
        p.push(Stage::Join {
            kind: JoinKind::Inner,
            table: "customers".into(),
            binding: "c".into(),
            strategy: Strategy::Hash,
            keys: 2,
            left_rows: 1000,
            right_rows: 500,
            out_rows: 1922,
            early_stopped: false,
        });
        let r = p.render();
        assert!(r[0].contains("Hash Join"), "{:?}", r[0]);
        assert!(r[0].contains("Inner"));
        assert!(r[0].contains("2 hash keys"));
        assert!(r[0].contains("customers c"));
        assert!(r[0].contains("actual rows=1922"));
    }

    #[test]
    fn a_join_with_no_key_says_so_because_that_is_why_it_is_slow() {
        let mut p = Plan::default();
        p.push(Stage::Join {
            kind: JoinKind::Inner,
            table: "customers".into(),
            binding: "customers".into(),
            strategy: Strategy::NestedLoop,
            keys: 0,
            left_rows: 1000,
            right_rows: 500,
            out_rows: 2500,
            early_stopped: false,
        });
        let r = p.render();
        assert!(r[0].contains("Nested Loop"));
        assert!(r[0].contains("no equality key"), "{:?}", r[0]);
    }

    #[test]
    fn early_termination_is_visible() {
        let mut p = Plan::default();
        p.push(Stage::Join {
            kind: JoinKind::Inner,
            table: "c".into(),
            binding: "c".into(),
            strategy: Strategy::Hash,
            keys: 1,
            left_rows: 1000,
            right_rows: 500,
            out_rows: 20,
            early_stopped: true,
        });
        p.budget = Some(20);
        let r = p.render();
        assert!(r[0].contains("stopped early"), "{:?}", r[0]);
        assert!(r.iter().any(|l| l.contains("Row budget: 20")));
    }

    #[test]
    fn filter_and_unique_report_what_they_removed() {
        let mut p = Plan::default();
        p.push(Stage::Filter { in_rows: 1000, out_rows: 117 });
        p.push(Stage::Distinct { in_rows: 117, out_rows: 4 });
        let r = p.render();
        assert!(r.iter().any(|l| l.contains("Unique") && l.contains("removed 113")), "{r:?}");
        assert!(r.iter().any(|l| l.contains("Filter") && l.contains("removed 883")), "{r:?}");
    }

    #[test]
    fn a_joins_two_inputs_are_siblings_not_nested() {
        // A join is the one stage with two inputs. Printing them at different
        // depths reads as "the left relation was scanned INSIDE the scan of
        // the right one", which is not what happened.
        let mut p = Plan::default();
        p.push(scan("orders", 10));
        p.push(scan("customers", 5));
        p.push(Stage::Join {
            kind: JoinKind::Inner,
            table: "customers".into(),
            binding: "customers".into(),
            strategy: Strategy::Hash,
            keys: 1,
            left_rows: 10,
            right_rows: 5,
            out_rows: 7,
            early_stopped: false,
        });
        let r = p.render();
        assert!(r[0].contains("Hash Join"), "{r:?}");
        let orders = r.iter().find(|l| l.contains("orders")).expect("orders scanned");
        let custs = r.iter().find(|l| l.contains("customers  (actual")).expect("customers");
        let depth = |l: &str| l.len() - l.trim_start().len();
        assert_eq!(
            depth(orders), depth(custs),
            "the two inputs of a join must be at the same depth\n{r:#?}"
        );
        assert!(depth(orders) > depth(&r[0]), "both are nested under the join");
    }

    #[test]
    fn joins_and_join_strategy_read_the_same_report() {
        let mut p = Plan::default();
        p.push(scan("a", 1));
        for s in [Strategy::NestedLoop, Strategy::Hash] {
            p.push(Stage::Join {
                kind: JoinKind::Left,
                table: "b".into(),
                binding: "b".into(),
                strategy: s,
                keys: 1,
                left_rows: 1,
                right_rows: 1,
                out_rows: 1,
                early_stopped: false,
            });
        }
        assert_eq!(p.joins().len(), 2);
        assert_eq!(p.join_strategy(0), Some(Strategy::NestedLoop));
        assert_eq!(p.join_strategy(1), Some(Strategy::Hash));
        assert_eq!(p.join_strategy(2), None);
    }
}
