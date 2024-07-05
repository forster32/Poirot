from collections import defaultdict
import copy
# import traceback
from concurrent.futures import ThreadPoolExecutor
import concurrent.futures

from loguru import logger
from tqdm import tqdm
# import networkx as nx

from agents.analyze_snippets import AnalyzeSnippetAgent
from config.client import PoirotConfig
from core.context_pruning import RepoContextManager, add_relevant_files_to_top_snippets, build_import_trees, parse_query_for_files
from core.entities import Snippet
from core.lexical_search import (
    compute_vector_search_scores,
    prepare_lexical_search_index,
    search_index,
)
from core.poirot_bot import context_get_files_to_change
from dataclass.separatedsnippets import SeparatedSnippets
from utils.repository_utils import Repository
from utils.streamable_functions import streamable
from utils.timer import Timer
from utils.multi_query import generate_multi_queries
from utils.openai_listwise_reranker import listwise_rerank_snippets


# the order here matters as the first match is used
code_snippet_separation_features = {
    "tools": {
        "prefix": [".git/", ".github/", ".circleci/", ".travis/", ".jenkins/", "scripts/", "script/", "bin/"],
        "suffix": [".gitignore", ".dockerignore", "Dockerfile", "Makefile", "Rakefile", "Procfile", ".sh", ".bat", ".cmd"],
        "substring": [],
    },
    "junk": { # we will discard this and not show it to the LLM
        "prefix": ["node_modules/", ".venv/", "build/", "venv/", "patch/", "target/", "bin/", "obj/"],
        "suffix": [".cache", ".gradle", ".mvn", ".settings", ".lock", ".log", ".tmp", ".tmp/", ".tmp.lock", ".tmp.lock/"],
        "substring": [".egg-info", "package-lock.json", "yarn.lock", ".cache", ".gradle", ".mvn"],
    },
    "dependencies": {
        "prefix": [".", "config/", ".github/", "vendor/"],
        "suffix": [".cfg", ".ini", ".po", "package.json", ".toml", ".yaml", ".yml", "LICENSE", ".lock"],
        "substring": ["requirements", "pyproject", "Gemfile", "Cargo", "pom.xml", "build.gradle"],
    },
    "docs": {
        "prefix": ["doc", "example", "README", "CHANGELOG"],
        "suffix": [".txt", ".rst", ".md", ".html", ".1", ".adoc", ".rdoc"],
        "substring": ["docs/", "documentation/"],
    },
    "tests": {
        "prefix": ["tests/", "test/", "spec/"],
        "suffix": [
            ".spec.ts", ".spec.js", ".test.ts", ".test.js",
            "_test.py", "_test.ts", "_test.js", "_test.go",
            "Test.java", "Tests.java", "Spec.java", "Specs.java",
            "_spec.rb", "_specs.rb", ".feature", "cy.ts", "cy.js"
        ],
        "substring": ["tests/", "test/", "/test", "_test", "rspec", ".test"],
    },
}

# otherwise it's tagged as source
# we can make a config category later for css, config.ts, config.js. so far config files aren't many.

type_to_percentile_floor = { # lower gets more snippets
    "tools": 0.3,
    "dependencies": 0.3,
    "docs": 0.3,
    "tests": 0.3,
    "source": 0.15, # very low floor for source code
}

type_to_score_floor = { # the lower, the more snippets. we set this higher for less used types
    "tools": 0.05,
    "dependencies": 0.025, # usually not matched, this won't hit often
    "docs": 0.20, # matched often, so we can set a high threshold
    "tests": 0.15, # matched often, so we can set a high threshold
    "source": 0.0, # very low floor for source code
}

type_to_result_count = {
    "tools": 5,
    "dependencies": 5,
    "docs": 5,
    "tests": 15,
    "source": 30,
}


rerank_count = {
    "tools": 10,
    "dependencies": 10,
    "docs": 30,
    "tests": 30,
    "source": 50, # have to decrease to 30 for Voyage AI
}


def apply_adjustment_score(
    snippet_path: str,
    old_score: float,
):
    snippet_score = old_score
    file_path, *_ = snippet_path.rsplit(":", 1)
    file_path = file_path.lower()
    # Penalize numbers as they are usually examples of:
    # 1. Test files (e.g. test_utils_3*.py)
    # 2. Generated files (from builds or snapshot tests)
    # 3. Versioned files (e.g. v1.2.3)
    # 4. Migration files (e.g. 2022_01_01_*.sql)
    base_file_name = file_path.split("/")[-1]
    if not base_file_name:
        return 0
    num_numbers = sum(c.isdigit() for c in base_file_name)
    snippet_score *= (1 - 1 / len(base_file_name)) ** num_numbers
    return snippet_score

VECTOR_SEARCH_WEIGHT = 2

@streamable
def multi_get_top_k_snippets(
    repository,
    queries: list[str],
    top_k: int = 15,
    seed: str = "", # for caches
):
    """
    Handles multiple queries at once now. Makes the vector search faster.
    """
    yield "Fetching configs...", [], [], []
    poirot_config: PoirotConfig = PoirotConfig()
    with Timer() as timer:
        for message, snippets, lexical_index in prepare_lexical_search_index.stream(
            repository.root_directory,
            poirot_config,
            seed=seed
        ):
            yield message, [], snippets, []
        logger.info(f"Lexical indexing took {timer.time_elapsed} seconds")
        for snippet in snippets:
            snippet.file_path = snippet.file_path[len(repository.root_directory) + 1 :]
            logger.info(f"snippet.file_path:{snippet.file_path}, repository.root_directory:{repository.root_directory}")
        yield "Searching lexical index...", [], snippets, []
        with Timer() as timer:
            content_to_lexical_score_list = [search_index(query, lexical_index) for query in queries]
        logger.info(f"Lexical search took {timer.time_elapsed} seconds")

    yield "Finished lexical search, performing vector search...", [], snippets, []
    assert content_to_lexical_score_list[0]

    with Timer() as timer:
        files_to_scores_list = compute_vector_search_scores(queries, snippets)
    logger.info(f"Vector search took {timer.time_elapsed} seconds")

    for i, query in enumerate(queries):
        for snippet in tqdm(snippets):
            vector_score = files_to_scores_list[i].get(snippet.denotation, 0.04)
            snippet_score = 0.02
            if snippet.denotation in content_to_lexical_score_list[i]:
                # roughly fine tuned vector score weight based on average score
                # from search_eval.py on 50 test cases May 13th, 2024 on an internal benchmark
                snippet_score = (content_to_lexical_score_list[i][snippet.denotation] + (
                    vector_score * VECTOR_SEARCH_WEIGHT
                )) / (VECTOR_SEARCH_WEIGHT + 1)
                content_to_lexical_score_list[i][snippet.denotation] = snippet_score
            else:
                content_to_lexical_score_list[i][snippet.denotation] = snippet_score * vector_score
            content_to_lexical_score_list[i][snippet.denotation] = apply_adjustment_score(
                snippet_path=snippet.denotation, old_score=content_to_lexical_score_list[i][snippet.denotation]
            )
    ranked_snippets_list = [
        sorted(
            snippets,
            key=lambda snippet: content_to_lexical_score[snippet.denotation],
            reverse=True,
        )[:top_k] for content_to_lexical_score in content_to_lexical_score_list
    ]
    yield "Finished hybrid search, currently performing reranking...", ranked_snippets_list, snippets, content_to_lexical_score_list


@streamable
def get_top_k_snippets(
    repository,
    query: str,
    top_k: int = 15,
    seed: str = "",
    *args,
    **kwargs,
):
    # Kinda cursed, we have to rework this
    for message, ranked_snippets_list, snippets, content_to_lexical_score_list in multi_get_top_k_snippets.stream(
        repository, [query], top_k, seed=seed
    ):
        yield message, ranked_snippets_list[0] if ranked_snippets_list else [], snippets, content_to_lexical_score_list[0] if content_to_lexical_score_list else []

def separate_snippets_by_type(snippets: list[Snippet]) -> SeparatedSnippets:
    separated_snippets = SeparatedSnippets()
    for snippet in snippets:
        for type_name, separation in code_snippet_separation_features.items():
            if any(snippet.file_path.startswith(prefix) for prefix in separation["prefix"]) or any(snippet.file_path.endswith(suffix) for suffix in separation["suffix"]) or any(substring in snippet.file_path for substring in separation["substring"]):
                separated_snippets.add_snippet(snippet, type_name)
                snippet.type_name = type_name
                break
        else:
            separated_snippets.add_snippet(snippet, "source")
    return separated_snippets


# TODO use COHERE_API_KEY or VOYAGE_API_KEY
def get_pointwise_reranked_snippet_scores(
    query: str,
    snippets: list[Snippet],
    snippet_scores: dict[str, float],
    NUM_SNIPPETS_TO_KEEP=5,
    NUM_SNIPPETS_TO_RERANK=100,
    directory_summaries: dict = {},
):
    """
    Ranks 1-5 snippets are frozen. They're just passed into Cohere since it helps with reranking. We multiply the scores by 1_000 to make them more significant.
    Ranks 6-100 are reranked using Cohere. Then we divide the scores by 1_000_000 to make them comparable to the original scores.
    """

    # if not COHERE_API_KEY and not VOYAGE_API_KEY:
    return snippet_scores

    # rerank_scores = copy.deepcopy(snippet_scores)

    # sorted_snippets = sorted(
    #     snippets,
    #     key=lambda snippet: rerank_scores[snippet.denotation],
    #     reverse=True,
    # )

    # snippet_representations = []
    # for snippet in sorted_snippets[:NUM_SNIPPETS_TO_RERANK]:
    #     representation = f"{snippet.file_path}\n```\n{snippet.get_snippet(add_lines=False, add_ellipsis=False)}\n```"
    #     subdirs = []
    #     for subdir in directory_summaries:
    #         if snippet.file_path.startswith(subdir):
    #             subdirs.append(subdir)
    #     subdirs = sorted(subdirs)
    #     for subdir in subdirs[-1:]:
    #         representation = representation + f"\n\nHere is a summary of the subdirectory {subdir}:\n\n" + directory_summaries[subdir]
    #     snippet_representations.append(representation)

    # # this needs to happen before we update the scores with the (higher) Cohere scores
    # snippet_denotations = set(snippet.denotation for snippet in sorted_snippets)
    # new_snippet_scores = {snippet_denotation: v / 1_000_000_000_000 for snippet_denotation, v in rerank_scores.items() if snippet_denotation in snippet_denotations}

    # if COHERE_API_KEY:
    #     response = cohere_rerank_call(
    #         query=query,
    #         documents=snippet_representations,
    #         max_chunks_per_doc=900 // NUM_SNIPPETS_TO_RERANK,
    #     )
    # elif VOYAGE_API_KEY:
    #     response = voyage_rerank_call(
    #         query=query,
    #         documents=snippet_representations,
    #     )
    # else:
    #     raise ValueError("No reranking API key found, use either Cohere or Voyage AI")

    # for document in response.results:
    #     new_snippet_scores[sorted_snippets[document.index].denotation] = apply_adjustment_score(
    #         snippet_path=sorted_snippets[document.index].denotation,
    #         old_score=document.relevance_score,
    #     )

    # for snippet in sorted_snippets[:NUM_SNIPPETS_TO_KEEP]:
    #     new_snippet_scores[snippet.denotation] = rerank_scores[snippet.denotation] * 1_000
    
    # # override score with Cohere score
    # for snippet in sorted_snippets[:NUM_SNIPPETS_TO_RERANK]:
    #     if snippet.denotation in new_snippet_scores:
    #         snippet.score = new_snippet_scores[snippet.denotation]
    # return new_snippet_scores


def process_snippets(type_name, *args, **kwargs):
    snippets_subset = args[1]
    if not snippets_subset:
        return type_name, {}
    return type_name, get_pointwise_reranked_snippet_scores(*args, **kwargs)

@streamable
def multi_prep_snippets(
    repository,
    queries,
    top_k: int = 15,
    skip_reranking: bool = False, # This is only for pointwise reranking
    skip_pointwise_reranking: bool = False,
    skip_analyze_agent: bool = False,
    NUM_SNIPPETS_TO_KEEP=0,
    NUM_SNIPPETS_TO_RERANK=100,
):
    """
    Assume 0th index is the main query.
    """
    if len(queries) > 1:
        logger.info("Using multi query...")
        for message, ranked_snippets_list, snippets, content_to_lexical_score_list in multi_get_top_k_snippets.stream(
            repository, queries, top_k * 3 # k * 3 to have enough snippets to rerank
        ):
            yield message, []
        # Use RRF to rerank snippets
        rank_fusion_offset = 0
        content_to_lexical_score = defaultdict(float)
        for i, ordered_snippets in enumerate(ranked_snippets_list):
            for j, snippet in enumerate(ordered_snippets):
                content_to_lexical_score[snippet.denotation] += content_to_lexical_score_list[i][snippet.denotation] * (1 / 2 ** (rank_fusion_offset + j))
        ranked_snippets = sorted(
            snippets,
            key=lambda snippet: content_to_lexical_score[snippet.denotation],
            reverse=True,
        )[:top_k]
    else:
        for message, ranked_snippets, snippets, content_to_lexical_score in get_top_k_snippets.stream(
            repository, queries[0], top_k
        ):
            yield message, ranked_snippets
    separated_snippets = separate_snippets_by_type(snippets)
    yield f"Retrieved top {top_k} snippets, currently reranking:\n", ranked_snippets
    if not skip_pointwise_reranking:
        all_snippets = []
        if "junk" in separated_snippets:
            separated_snippets.override_list("junk", [])
        for type_name, snippets_subset in separated_snippets:
            if len(snippets_subset) == 0:
                continue
            separated_snippets.override_list(type_name, sorted(
                snippets_subset,
                key=lambda snippet: content_to_lexical_score[snippet.denotation],
                reverse=True,
            )[:rerank_count[type_name]])
        new_content_to_lexical_score_by_type = {}

        with Timer() as timer:
            try:
                with ThreadPoolExecutor() as executor:
                    future_to_type = {executor.submit(process_snippets, type_name, queries[0], snippets_subset, content_to_lexical_score, NUM_SNIPPETS_TO_KEEP, rerank_count[type_name], {}): type_name for type_name, snippets_subset in separated_snippets}
                    for future in concurrent.futures.as_completed(future_to_type):
                        type_name = future_to_type[future]
                        new_content_to_lexical_score_by_type[type_name] = future.result()[1]
            except RuntimeError as e:
                # Fallback to sequential processing
                logger.warning(e)
                for type_name, snippets_subset in separated_snippets:
                    new_content_to_lexical_score_by_type[type_name] = process_snippets(type_name, queries[0], snippets_subset, content_to_lexical_score, NUM_SNIPPETS_TO_KEEP, rerank_count[type_name], {})[1]
        logger.info(f"Reranked snippets took {timer.time_elapsed} seconds")

        for type_name, snippets_subset in separated_snippets:
            new_content_to_lexical_scores = new_content_to_lexical_score_by_type[type_name]
            for snippet in snippets_subset:
                snippet.score = new_content_to_lexical_scores[snippet.denotation]
            # set all keys of new_content_to_lexical_scores to content_to_lexical_score
            for key in new_content_to_lexical_scores:
                content_to_lexical_score[key] = new_content_to_lexical_scores[key]
            snippets_subset = sorted(
                snippets_subset,
                key=lambda snippet: new_content_to_lexical_scores[snippet.denotation],
                reverse=True,
            )
            separated_snippets.override_list(attribute_name=type_name, new_list=snippets_subset)
            logger.info(f"Reranked {type_name}")
            # cutoff snippets at percentile
            logger.info("Kept these snippets")
            if not snippets_subset:
                continue
            top_score = snippets_subset[0].score
            logger.debug(f"Top score for {type_name}: {top_score}")
            max_results = type_to_result_count[type_name]
            filtered_subset_snippets = []
            for idx, snippet in enumerate(snippets_subset[:max_results]):
                percentile = 0 if top_score == 0 else snippet.score / top_score
                if percentile < type_to_percentile_floor[type_name] or snippet.score < type_to_score_floor[type_name]:
                    break 
                logger.info(f"{idx}: {snippet.denotation} {snippet.score} {percentile}")
                snippet.type_name = type_name
                filtered_subset_snippets.append(snippet)
            if type_name != "source" and filtered_subset_snippets and not skip_analyze_agent: # do more filtering
                filtered_subset_snippets = AnalyzeSnippetAgent().analyze_snippets(filtered_subset_snippets, type_name, queries[0])
            logger.info(f"Length of filtered subset snippets for {type_name}: {len(filtered_subset_snippets)}")
            all_snippets.extend(filtered_subset_snippets)
        # if there are no snippets because all of them have been filtered out we will fall back to adding the highest rated ones
        # only do this for source files
        if not all_snippets:
            for type_name, snippets_subset in separated_snippets:
                # only use source files unless there are none in which case use all snippets
                if type_name != "source" and separated_snippets.source:
                    continue
                max_results = type_to_result_count[type_name]
                all_snippets.extend(snippets_subset[:max_results])

        all_snippets.sort(key=lambda snippet: snippet.score, reverse=True)
        ranked_snippets = all_snippets[:top_k]
        yield "Finished reranking, here are the relevant final search results:\n", ranked_snippets
    else:
        ranked_snippets = sorted(
            snippets,
            key=lambda snippet: content_to_lexical_score[snippet.denotation],
            reverse=True,
        )[:top_k]
        yield "Finished reranking, here are the relevant final search results:\n", ranked_snippets
    # you can use snippet.denotation and snippet.get_snippet()
    # TODO check skip_pointwise_reranking
    if not skip_reranking and skip_pointwise_reranking:
        ranked_snippets[:NUM_SNIPPETS_TO_RERANK] = listwise_rerank_snippets(queries[0], ranked_snippets[:NUM_SNIPPETS_TO_RERANK])
    return ranked_snippets

@streamable
def prep_snippets(
    repository,
    query,
    top_k: int = 15,
) -> list[Snippet]:
    queries = [query, *generate_multi_queries(query)]
    yield "Finished generating search queries, performing lexical search...\n", []

    for message, snippets in multi_prep_snippets.stream(
        repository, queries, top_k
    ):
        yield message, snippets
    return snippets


def get_relevant_context(
    query: str,
    repo_context_manager: RepoContextManager,
    # seed: int = None,
    # import_graph: nx.DiGraph = None,
    # chat_logger = None,
    # images = None
) -> RepoContextManager:
    # logger.info("Seed: " + str(seed))
    repo_context_manager = build_import_trees(
        repo_context_manager,
        # import_graph,
    )
    repo_context_manager = add_relevant_files_to_top_snippets(repo_context_manager)
    # Idea: make two passes, one with tests and one without
    # if editing source code only provide source code
    # if editing test provide both source and test code
    relevant_files, read_only_files = context_get_files_to_change(
        relevant_snippets=repo_context_manager.current_top_snippets,
        read_only_snippets=repo_context_manager.read_only_snippets,
        problem_statement=query,
        # import_graph=import_graph,
        # chat_logger=chat_logger,
        # seed=seed,
        # images=images
    )
    previous_top_snippets = copy.deepcopy(repo_context_manager.current_top_snippets)
    previous_read_only_snippets = copy.deepcopy(repo_context_manager.read_only_snippets)
    repo_context_manager.current_top_snippets = []
    repo_context_manager.read_only_snippets = []
    for relevant_file in relevant_files:
        try:
            content = repo_context_manager.repository.get_file_contents(relevant_file)
        except FileNotFoundError:
            continue
        snippet = Snippet(
            file_path=relevant_file,
            start=0,
            end=len(content.split("\n")),
            content=content,
        )
        repo_context_manager.current_top_snippets.append(snippet)
    for read_only_file in read_only_files:
        try:
            content = repo_context_manager.repository.get_file_contents(read_only_file)
        except FileNotFoundError:
            continue
        snippet = Snippet(
            file_path=read_only_file,
            start=0,
            end=len(content.split("\n")),
            content=content,
        )
        repo_context_manager.read_only_snippets.append(snippet)
    if not repo_context_manager.current_top_snippets and not repo_context_manager.read_only_snippets:
        repo_context_manager.current_top_snippets = copy.deepcopy(previous_top_snippets)
        repo_context_manager.read_only_snippets = copy.deepcopy(previous_read_only_snippets)
    return repo_context_manager

@streamable
def fetch_relevant_files(
    repository,
    title,
    summary,
    replies_text,
):
    logger.info("Fetching relevant files")
    # try:
    search_query = f"{title}\n{summary}\n{replies_text}".strip("\n")
    replies_text = f"\n{replies_text}" if replies_text else ""
    formatted_query = (f"{title.strip()}\n{summary.strip()}" + replies_text).strip(
        "\n"
    )
    
    for message, ranked_snippets in prep_snippets.stream(repository, search_query):
        repo_context_manager = RepoContextManager(
            repository=repository,
            current_top_snippets=ranked_snippets,
            read_only_snippets=[],
        )
        yield message, repo_context_manager
    

    parse_query_for_files(search_query, repo_context_manager)
    repo_context_manager = add_relevant_files_to_top_snippets(repo_context_manager)

    yield "Here are the files I've found so far. I'm currently selecting a subset of the files to edit.\n", repo_context_manager

    repo_context_manager = get_relevant_context(
        formatted_query,
        repo_context_manager,
        # ticket_progress,
        # chat_logger=chat_logger,
        # import_graph=import_graph,
        # images=images
    )
    yield "Here are the code search results. I'm now analyzing these search results to write the PR.\n", repo_context_manager
    # except Exception as e:
        # logger.exception()
    #     raise e
    return repo_context_manager


if __name__ == "__main__":
    import os

    repository_directory = os.getenv("ROOT_DIRECTORY")

    # title = "when I call openai chat, I want to get the token cost of the completion."
    # summary = "I want to get the token cost of the completion when I call openai chat. Please help me with this."

    title = "I want to add a field called 'description' to the table database table 'thread'."

    # title = "我想要增加一个字段：用户级别，在用户创建 thread 数量达到一定数量时，用户级别会提升。"


    summary = ""
    replies_text=""

    repository = Repository(repository_directory)

    logger.info("Fetching relevant files")
    for message, repo_context_manager in fetch_relevant_files.stream(repository, title, summary, replies_text):
      logger.info(message)

    print("-----return---------")
    print("current_top_snippets: ", repo_context_manager.current_top_snippets)
    print("read_only_snippets: ", repo_context_manager.read_only_snippets)
    

    print("-----end---------")
