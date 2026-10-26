import { z } from 'zod';

const workspace = z.string().min(1).max(4096).optional()
  .describe('Existing absolute working directory. This does not grant trust to project skills.');
const goal = z.string().trim().min(1).max(4000);
const skillId = z.string().min(1).max(512);
const requestedSkills = z.array(skillId).max(3).optional()
  .describe('Explicit skill IDs from discovery. Omit for goal-based selection; [] selects no skills.');
const taskSkills = z.array(z.string().min(1).max(256)).max(3).optional()
  .describe('Explicit skill IDs for this task. Omit for selection by goal; [] selects no skills.');
const taskId = z.string().min(1).max(128);
const checkId = z.string().min(1).max(400);

// These are expectations supplied by the caller, not conclusions derived from
// the artifact. The runtime pins and validates them against the check kind.
export const checkExpectationsSchema = z.object({
  required_columns: z.array(z.string().min(1).max(256)).min(1).max(256)
    .refine(value => new Set(value).size === value.length, 'Required columns must be unique.').optional(),
  min_rows: z.number().int().min(0).max(100000).optional(),
  delimiter: z.string().length(1).refine(value => !['\r', '\n', '\0', '"'].includes(value),
    'Delimiter cannot be newline, NUL or quote.').optional(),
  min_bytes: z.number().int().min(0).max(16 * 1024 * 1024).optional(),
}).strict();

export const prepareTaskSchema = z.object({
  goal,
  workspace,
  requested_skills: taskSkills,
  check_inputs: z.record(checkId, checkExpectationsSchema)
    .refine(value => Object.keys(value).length <= 96, 'At most 96 check inputs are allowed.').optional()
    .describe('Expected output properties keyed by the qualified check ID (<skill ID>/<local check ID>).'),
}).strict();

export const skillsRequestSchema = z.discriminatedUnion('action', [
  z.object({ action: z.literal('list'), workspace }).strict(),
  z.object({
    action: z.literal('resolve'), goal, workspace,
    requested_skills: requestedSkills,
    limit: z.number().int().min(1).max(3).optional(),
  }).strict(),
  z.object({
    action: z.literal('read'), skill_id: skillId, workspace,
    resource: z.string().min(1).max(512).optional()
      .describe('Relative bundled text resource; omit to load SKILL.md and its completion contract.'),
    expected_hash: z.string().regex(/^[a-fA-F0-9]{64}$/).optional()
      .describe('Expected package SHA-256 from discovery. A changed package is rejected.'),
  }).strict(),
]);

const checkRequest = z.object({
  action: z.literal('check'),
  task_id: taskId,
  check_id: checkId,
  path: z.string().min(1).max(4096).optional()
    .describe('Artifact path within the task workspace. For automatic checks only.'),
  status: z.enum(['passed', 'failed', 'not_applicable', 'unknown']).optional(),
  evidence: z.string().trim().min(1).max(4000).optional()
    .describe('Observed result or reference for a manual check. Stored as self-reported evidence.'),
  reason: z.string().trim().min(1).max(2000).optional(),
}).strict().superRefine((request, ctx) => {
  const issue = (path, message) => ctx.addIssue({ code: 'custom', path: [path], message });
  if (request.path !== undefined) {
    for (const key of ['status', 'evidence', 'reason']) {
      if (request[key] !== undefined) issue(key, 'Automatic artifact checks accept path, not a supplied verdict.');
    }
  } else {
    if (request.status === undefined) issue('status', 'A manual check requires status.');
    if (request.evidence === undefined) issue('evidence', 'A manual check requires evidence.');
    if (request.status === 'not_applicable' && request.reason === undefined) {
      issue('reason', 'not_applicable requires a reason; the contract must also allow it.');
    }
  }
});

export const taskRequestSchema = z.discriminatedUnion('action', [
  z.object({ action: z.literal('status'), task_id: taskId }).strict(),
  z.object({ action: z.literal('resume'), task_id: taskId }).strict(),
  z.object({ action: z.literal('finish'), task_id: taskId }).strict(),
  checkRequest,
]);

export function registerTaskTools(server, callAction) {
  server.registerTool('desktop_skills', {
    title: 'Discover and Read Workstation Skills',
    description: 'List trusted skill metadata, resolve relevant methods for a goal, or read a selected SKILL.md '
      + 'and its completion contract. Read bundled references only as needed. Discovery never runs scripts; '
      + 'a skill cannot grant privileges. Use IDs and hashes from list/resolve. Workspace trust is configured locally.',
    inputSchema: z.object({ request: skillsRequestSchema }).strict(),
    annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
  }, async ({ request }) => {
    const input = skillsRequestSchema.parse(request);
    return callAction({ ...input, action: `skill_${input.action}` });
  });

  server.registerTool('desktop_prepare_task', {
    title: 'Prepare a Workstation Task with Skills',
    description: 'Start a goal-based workflow: choose relevant skills, load their instructions, pin versions and '
      + 'completion checks, and create a persistent task ID. Optional check_inputs declare the expected output. '
      + 'No application actions run and no permissions are granted. No matching skill is a valid result; '
      + 'read the returned limits. Each call creates a new task; use desktop_task resume for an existing one.',
    inputSchema: prepareTaskSchema,
    annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: false },
  }, async args => callAction({ action: 'task_prepare', ...prepareTaskSchema.parse(args) }));

  server.registerTool('desktop_task', {
    title: 'Track and Verify a Workstation Task',
    description: 'status reads progress; check records a manual result or verifies a workspace artifact; '
      + 'finish reports whether required checks are satisfied and distinguishes self-reported from machine-verified evidence; '
      + 'resume returns pinned instructions and checkpoints without replaying actions. For automatic checks supply path, '
      + 'never a passed verdict; output expectations must be declared during preparation. Inspect completed/state/assurance in the result: '
      + 'a successful tool call alone does not mean the task is complete. Re-observe application targets after resuming.',
    inputSchema: z.object({ request: taskRequestSchema }).strict(),
    annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: false },
  }, async ({ request }) => {
    const input = taskRequestSchema.parse(request);
    return callAction({ ...input, action: `task_${input.action}` });
  });
}
