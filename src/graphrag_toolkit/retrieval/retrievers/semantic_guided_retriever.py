# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import logging
from collections import defaultdict
from typing import List, Optional, Any, Union, Type

from llama_index.core.base.base_retriever import BaseRetriever
from llama_index.core.schema import NodeWithScore, QueryBundle, TextNode
from llama_index.core.async_utils import run_async_tasks

from graphrag_toolkit.storage.graph_store import GraphStore
from graphrag_toolkit.storage import VectorStore

from graphrag_toolkit.retrieval.retrievers.semantic_guided_base_retriever import SemanticGuidedBaseRetriever
from graphrag_toolkit.retrieval.retrievers.keyword_ranking_search import KeywordRankingSearch
from graphrag_toolkit.retrieval.retrievers.statement_cosine_seach import StatementCosineSimilaritySearch
from graphrag_toolkit.retrieval.retrievers.semantic_beam_search import SemanticBeamGraphSearch
from graphrag_toolkit.retrieval.retrievers.rerank_beam_search import RerankBeamSearchRetriever
from graphrag_toolkit.retrieval.utils.statement_utils import get_statements_query, SharedEmbeddingCache

logger = logging.getLogger(__name__)

SemanticGuidedRetrieverType = Union[SemanticGuidedBaseRetriever, Type[SemanticGuidedBaseRetriever]]

class SemanticGuidedRetriever(SemanticGuidedBaseRetriever):
    def __init__(
        self,
        vector_store: VectorStore,
        graph_store: GraphStore,
        retrievers: Optional[List[Union[SemanticGuidedBaseRetriever, Type[SemanticGuidedBaseRetriever]]]] = None,
        share_results: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(vector_store, graph_store, **kwargs)
        self.share_results = share_results
        
        # Create shared embedding cache
        self.shared_embedding_cache = SharedEmbeddingCache(vector_store)

        self.initial_retrievers = []
        self.graph_retrievers = []
        
        # initialize retrievers
        if retrievers:
            for retriever in retrievers:
                if isinstance(retriever, type):
                    instance = retriever(
                        vector_store, 
                        graph_store, 
                        **kwargs
                    )
                else:
                    instance = retriever
                
                # Inject shared cache if not already set
                if hasattr(instance, 'embedding_cache') and instance.embedding_cache is None:
                    instance.embedding_cache = self.shared_embedding_cache
                
                if isinstance(instance, (SemanticBeamGraphSearch, RerankBeamSearchRetriever)):
                    self.graph_retrievers.append(instance)
                else:
                    self.initial_retrievers.append(instance)
        else:
            # Default configuration
            self.initial_retrievers = [
                StatementCosineSimilaritySearch(
                    vector_store=vector_store,
                    graph_store=graph_store,
                    embedding_cache=self.shared_embedding_cache,
                    **kwargs
                ),
                KeywordRankingSearch(
                    vector_store=vector_store,
                    graph_store=graph_store,
                    embedding_cache=self.shared_embedding_cache,
                    **kwargs
                )
            ]

    def _retrieve(self, query_bundle: QueryBundle) -> List[NodeWithScore]:
        try:
            # 1. Get initial results in parallel
            tasks = [r.aretrieve(query_bundle) for r in self.initial_retrievers]
            initial_results = run_async_tasks(tasks)

            # 2. Collect unique initial nodes
            seen_statement_ids = set()
            initial_nodes = []
            for nodes in initial_results:
                for node in nodes:
                    statement_id = node.node.metadata['statement']['statementId']
                    if statement_id not in seen_statement_ids:
                        seen_statement_ids.add(statement_id)
                        initial_nodes.append(node)

            all_nodes = initial_nodes.copy()

            # 3. Graph expansion if enabled
            if self.share_results and initial_nodes:
                for retriever in self.graph_retrievers:
                    try:
                        retriever.shared_nodes = initial_nodes
                        graph_nodes = retriever.retrieve(query_bundle)
                        for node in graph_nodes:
                            statement_id = node.node.metadata['statement']['statementId']
                            if statement_id not in seen_statement_ids:
                                seen_statement_ids.add(statement_id)
                                all_nodes.append(node)
                    except Exception as e:
                        logger.error(f"Error in graph retriever {retriever.__class__.__name__}: {e}")
                        continue

            # 4. Fetch statements once
            if not all_nodes:
                return []

            statement_ids = [
                node.node.metadata['statement']['statementId'] 
                for node in all_nodes
            ]
            statements = get_statements_query(self.graph_store, statement_ids)

            # 5. Create final nodes with full data
            final_nodes = []
            statements_map = {
                s['result']['statement']['statementId']: s['result'] 
                for s in statements
            }
            
            for node in all_nodes:
                statement_id = node.node.metadata['statement']['statementId']
                if statement_id in statements_map:
                    result = statements_map[statement_id]
                    new_node = TextNode(
                        text=result['statement']['value'],
                        metadata={
                            **node.node.metadata,  # Preserve retriever metadata
                            'statement': result['statement'],
                            'chunk': result['chunk'],
                            'source': result['source']                     
                        }
                    )
                    final_nodes.append(NodeWithScore(
                        node=new_node,
                        score=node.score
                    ))

            # 6. Group by source for better context
            source_nodes = defaultdict(list)
            for node in final_nodes:
                source_id = node.node.metadata['source']['sourceId']
                source_nodes[source_id].append(node)

            # 7. Create final ordered list
            ordered_nodes = []
            for source_id, nodes in source_nodes.items():
                nodes.sort(key=lambda x: x.score or 0.0, reverse=True)
                ordered_nodes.extend(nodes)

            return ordered_nodes

        except Exception as e:
            logger.error(f"Error in StatementGraphRetriever: {e}")
            return []